"""Create, exercise, and always destroy one real AI-Q Sprites sandbox."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType
from uuid import uuid4


def _load_minimal_aiq_contract() -> None:
    """Bypass unrelated AI-Q agent imports when testing from a source checkout."""
    raw_source = os.environ.get("AIQ_SOURCE_DIR")
    if not raw_source:
        return
    source = Path(raw_source).resolve()
    sys.path.insert(0, str(source))
    package_paths = {
        "aiq_agent.agents": source / "aiq_agent" / "agents",
        "aiq_agent.agents.deep_researcher": source / "aiq_agent" / "agents" / "deep_researcher",
        "aiq_agent.agents.deep_researcher.sandbox": source / "aiq_agent" / "agents" / "deep_researcher" / "sandbox",
    }
    for name, path in package_paths.items():
        package = ModuleType(name)
        package.__path__ = [str(path)]  # type: ignore[attr-defined]
        sys.modules[name] = package


_load_minimal_aiq_contract()

from aiq_agent.agents.deep_researcher.sandbox.config import SandboxConfig  # noqa: E402
from aiq_agent.agents.deep_researcher.sandbox.registry import create_sandbox_backend  # noqa: E402
from sprites import NotFoundError, SpritesClient  # noqa: E402


def main() -> None:
    token = os.environ.get("SPRITE_TOKEN", "").strip()
    if not token:
        raise SystemExit("SPRITE_TOKEN is required")

    job_id = f"sprites-provider-smoke-{uuid4()}"
    backend = create_sandbox_backend(
        SandboxConfig.model_validate(
            {
                "provider": "sprites",
                "workdir": "/workspace",
                "network": {"mode": "blocked"},
                "timeout": 300,
                "artifact_capture": {"enabled": True, "max_file_bytes": 1_000_000},
            }
        ),
        job_id,
    )
    sprite_name = backend.sandbox_name
    try:
        result = backend.execute("python3 -c \"print('hello from AI-Q on Sprites')\"")
        assert result.exit_code == 0, result
        assert "hello from AI-Q on Sprites" in result.output

        input_path = f"{backend.workdir}/input.txt"
        uploads = backend.upload_files([(input_path, b"sprite-artifact\n")])
        assert uploads[0].error is None, uploads
        downloads = backend.download_files([input_path])
        assert downloads[0].error is None, downloads
        assert downloads[0].content == b"sprite-artifact\n"

        egress = backend.execute("python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), timeout=2)\"")
        assert egress.exit_code != 0, "blocked sandbox unexpectedly reached 1.1.1.1:443"
        print(f"PASS: exec, transfer, and blocked egress on {sprite_name}")
    finally:
        backend.terminate()

    with SpritesClient(token=token) as client:
        try:
            client.get_sprite(sprite_name)
        except NotFoundError:
            print(f"PASS: cleanup confirmed for {sprite_name}")
        else:
            raise AssertionError(f"Sprite still exists after provider termination: {sprite_name}")


if __name__ == "__main__":
    main()
