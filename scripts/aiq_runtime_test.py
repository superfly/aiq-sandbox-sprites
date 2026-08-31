"""Exercise the installed wheel through AI-Q's real runtime and artifact lifecycle."""

from __future__ import annotations

import base64
import os
import shlex
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from aiq_agent.agents.deep_researcher.deepagents_runtime import DeepAgentsRuntime, DeepResearchSandboxConfig
from aiq_agent.agents.deep_researcher.sandbox.base import SandboxTerminatedError
from sprites import NotFoundError, SpritesClient


def _assert_absent(name: str, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    with SpritesClient(token=os.environ["SPRITE_TOKEN"]) as client:
        while True:
            try:
                client.get_sprite(name)
            except NotFoundError:
                return
            if time.monotonic() >= deadline:
                raise AssertionError(f"AI-Q runtime left a disposable Sprite behind: {name}")
            time.sleep(0.25)


def _sandbox_config(*, artifact_capture: bool) -> DeepResearchSandboxConfig:
    return DeepResearchSandboxConfig.model_validate(
        {
            "provider": "sprites",
            "workdir": "/workspace",
            "network": "blocked",
            "timeout": 90,
            "idle_timeout": 90,
            "artifact_capture": {
                "enabled": artifact_capture,
                "max_file_bytes": 1_000_000,
                "max_total_bytes": 5_000_000,
                "max_file_count": 10,
                "allow_extensions": [".csv", ".json", ".md"],
            },
        }
    )


def _sandbox_provider(runtime: DeepAgentsRuntime) -> object:
    """Read the provider across AI-Q 2.2 rc6 and current develop test surfaces."""
    provider = getattr(runtime, "sandbox_backend", None)
    if provider is None:
        provider = getattr(runtime, "_sandbox_provider", None)
    assert provider is not None
    return provider


def _write_artifacts_command(artifact_dir: str) -> str:
    csv_path = f"{artifact_dir}/analysis.csv"
    manifest_path = f"{artifact_dir}/manifest.json"
    csv_bytes = b"category,value\nalpha,2\nbeta,3\n"
    manifest_bytes = (
        '{"version":1,"artifacts":[{"path":"' + csv_path + '","kind":"table","title":"Release QA table"}]}'
    ).encode()
    script = (
        "import base64; from pathlib import Path; "
        f"Path({artifact_dir!r}).mkdir(parents=True, exist_ok=True); "
        f"Path({csv_path!r}).write_bytes(base64.b64decode({base64.b64encode(csv_bytes).decode()!r})); "
        f"Path({manifest_path!r}).write_bytes(base64.b64decode({base64.b64encode(manifest_bytes).decode()!r}))"
    )
    return f"python3 -c {shlex.quote(script)}"


def _test_artifact_runtime() -> None:
    events: list[dict[str, object]] = []
    job_id = f"aiq-runtime-artifact-{uuid4()}"
    with tempfile.TemporaryDirectory(prefix="aiq-sprites-artifacts-") as temp_dir:
        database = Path(temp_dir) / "jobs.db"
        runtime = DeepAgentsRuntime(
            sandbox=_sandbox_config(artifact_capture=True),
            job_id=job_id,
            artifact_db_url=f"sqlite:///{database}",
            artifact_emit=events.append,
        )
        provider = _sandbox_provider(runtime)
        name = provider.sandbox_name  # type: ignore[attr-defined]
        try:
            result = runtime.backend.execute(_write_artifacts_command(runtime.artifact_dir))
            assert result.exit_code == 0, result.output

            manager = runtime.artifact_manager
            assert manager is not None
            checkpointed = manager.harvest_after_execute()
            assert [artifact.filename for artifact in checkpointed] == ["analysis.csv"]
            stored_csv = checkpointed[0]
            assert b"".join(manager.store.open_bytes(job_id, stored_csv.artifact_id)) == (
                b"category,value\nalpha,2\nbeta,3\n"
            )

            notes_path = f"{runtime.artifact_dir}/notes.md"
            notes = runtime.backend.execute(f"printf 'release notes\\n' > {shlex.quote(notes_path)}")
            assert notes.exit_code == 0, notes.output
            assert runtime.finalize_artifacts(interrupted=False) is True

            stored = manager.store.list(job_id)
            assert sorted(artifact.filename for artifact in stored) == ["analysis.csv", "notes.md"]
            report = manager.resolve_report_references("![table](artifact://analysis.csv)", stored)
            assert f"artifact://{stored_csv.artifact_id}" in report
            indexed = manager.append_artifact_index("# Report", stored)
            assert "analysis.csv" in indexed and "notes.md" in indexed
        finally:
            assert runtime.finalize(interrupted=False) is True

        _assert_absent(name)
        artifact_events = [event for event in events if event.get("type") == "artifact.update"]
        cleanup_events = [event for event in events if event.get("type") == "sandbox.cleanup"]
        assert len(artifact_events) == 2
        assert [event["data"]["status"] for event in cleanup_events] == ["started", "succeeded"]  # type: ignore[index]
        assert all(event["data"]["provider"] == "sprites" for event in cleanup_events)  # type: ignore[index]
        assert os.environ["SPRITE_TOKEN"] not in repr(events)
        print("PASS: AI-Q runtime artifact checkpoint, final scan, storage, events, and cleanup")


def _test_interrupted_runtime() -> None:
    events: list[dict[str, object]] = []
    job_id = f"aiq-runtime-interrupt-{uuid4()}"
    runtime = DeepAgentsRuntime(
        sandbox=_sandbox_config(artifact_capture=False),
        job_id=job_id,
        artifact_emit=events.append,
    )
    provider = _sandbox_provider(runtime)
    name = provider.sandbox_name  # type: ignore[attr-defined]
    assert runtime.backend.execute("printf ready").output == "ready"
    outcomes: list[object] = []

    def _execute_long_command() -> None:
        try:
            outcomes.append(runtime.backend.execute("sleep 45; printf unexpected", timeout=45))
        except Exception as exc:  # noqa: BLE001 - teardown may surface a normalized or transport failure
            outcomes.append(exc)

    worker = threading.Thread(target=_execute_long_command, daemon=True)
    worker.start()
    time.sleep(1.0)
    assert runtime.finalize_artifacts(interrupted=True) is False
    assert runtime.finalize(interrupted=True) is True
    worker.join(timeout=15)
    assert not worker.is_alive()
    assert outcomes
    outcome = outcomes[0]
    if not isinstance(outcome, Exception):
        assert outcome.exit_code != 0  # type: ignore[attr-defined]
    with _expect_terminated():
        runtime.backend.execute("true")
    _assert_absent(name)

    cleanup_events = [event for event in events if event.get("type") == "sandbox.cleanup"]
    assert [event["data"]["status"] for event in cleanup_events] == ["started", "succeeded"]  # type: ignore[index]
    assert all(event["data"]["interrupted"] is True for event in cleanup_events)  # type: ignore[index]
    assert os.environ["SPRITE_TOKEN"] not in repr(events)
    print("PASS: AI-Q interrupted finalization, sanitized lifecycle events, and cleanup")


@contextmanager
def _expect_terminated() -> Iterator[None]:
    """Keep the standalone check independent from pytest."""
    try:
        yield
    except SandboxTerminatedError:
        return
    raise AssertionError("expected SandboxTerminatedError")


def main() -> None:
    if not os.environ.get("SPRITE_TOKEN", "").strip():
        raise SystemExit("SPRITE_TOKEN is required")
    _test_artifact_runtime()
    _test_interrupted_runtime()


if __name__ == "__main__":
    main()
