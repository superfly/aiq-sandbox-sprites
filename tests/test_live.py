"""Opt-in release checks against disposable Sprites in the sprites-test org."""

from __future__ import annotations

import os
import shlex
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from aiq_agent.agents.deep_researcher.sandbox.base import SandboxTerminatedError
from aiq_agent.agents.deep_researcher.sandbox.config import SandboxConfig
from sprites import NotFoundError, SpritesClient

from aiq_sprites.provider import (
    SpriteBootstrapError,
    SpriteCreationError,
    SpritesSandboxProvider,
    _ownership_label,
)
from aiq_sprites.settings import SpritesSettings

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("AIQ_SPRITES_LIVE_TEST") != "1",
        reason="set AIQ_SPRITES_LIVE_TEST=1 to create disposable Sprites",
    ),
]


def _token() -> str:
    token = os.environ.get("SPRITE_TOKEN", "").strip()
    if not token:
        pytest.skip("SPRITE_TOKEN is required for live tests")
    return token


def _config(
    *,
    network: str = "blocked",
    network_allow: tuple[str, ...] = (),
    max_file_bytes: int = 50_000_000,
) -> SandboxConfig:
    return SandboxConfig.model_validate(
        {
            "provider": "sprites",
            "workdir": "/workspace",
            "network": {"mode": network, "allow": network_allow},
            "timeout": 90,
            "idle_timeout": 90,
            "artifact_capture": {"enabled": True, "max_file_bytes": max_file_bytes},
        }
    )


def _settings(**overrides: object) -> SpritesSettings:
    values: dict[str, object] = {
        "token": _token(),
        "runtime": "dev",
        "max_output_bytes": 4096,
        "max_download_bytes": 50_000_000,
        "api_timeout_seconds": 30.0,
        "bootstrap_timeout_seconds": 120,
        "verify_network": True,
        "network_verify_timeout_seconds": 15.0,
        "python_packages": (),
    }
    values.update(overrides)
    return SpritesSettings(**values)  # type: ignore[arg-type]


def _provider(
    *,
    network: str = "blocked",
    network_allow: tuple[str, ...] = (),
    max_file_bytes: int = 50_000_000,
    settings: SpritesSettings | None = None,
    job_id: str | None = None,
) -> SpritesSandboxProvider:
    return SpritesSandboxProvider(
        _config(network=network, network_allow=network_allow, max_file_bytes=max_file_bytes),
        job_id or f"live-{uuid4()}",
        settings=settings or _settings(),
    )


def _assert_absent(name: str, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    with SpritesClient(token=_token()) as client:
        while True:
            try:
                client.get_sprite(name)
            except NotFoundError:
                return
            if time.monotonic() >= deadline:
                pytest.fail(f"disposable Sprite was not deleted: {name}")
            time.sleep(0.25)


@contextmanager
def _managed(provider: SpritesSandboxProvider) -> Iterator[SpritesSandboxProvider]:
    try:
        yield provider
    finally:
        provider.terminate()
        _assert_absent(provider.sandbox_name)


def _tcp_probe(host: str, port: int = 443) -> str:
    return (
        'python3 -c "import socket; '
        f"s=socket.create_connection(('{host}', {port}), timeout=4); "
        "s.close(); print('connected')\""
    )


def test_blocked_execution_transfer_limits_timeout_and_cleanup() -> None:
    provider = _provider(
        max_file_bytes=1024,
        settings=_settings(max_download_bytes=1024),
    )
    with _managed(provider):
        success = provider.execute("printf 'hello-from-live-sprite\\n'")
        assert success.exit_code == 0
        assert success.output == "hello-from-live-sprite\n"

        # Host credentials must never become part of the generated-code environment.
        no_token = provider.execute("python3 -c \"import os; assert 'SPRITE_TOKEN' not in os.environ\"")
        assert no_token.exit_code == 0

        payload = bytes(range(256)) * 2
        path = f"{provider.workdir}/binary.dat"
        uploaded = provider.upload_files([(path, payload)])
        assert uploaded[0].error is None
        downloaded = provider.download_files([path])
        assert downloaded[0].error is None
        assert downloaded[0].content == payload

        oversized_path = f"{provider.workdir}/oversized.bin"
        made_large = provider.execute(
            f"python3 -c \"from pathlib import Path; Path('{oversized_path}').write_bytes(b'x' * 2048)\""
        )
        assert made_large.exit_code == 0
        oversized = provider.download_files([oversized_path])
        assert oversized[0].error == "file_too_large"
        assert oversized[0].content is None

        bounded = provider.execute("python3 -c \"print('x' * 20000)\"; exit 17")
        assert bounded.exit_code == 17
        assert bounded.truncated is True
        assert len(bounded.output.encode()) <= 4096
        assert "output truncated" in bounded.output

        timeout_marker = f"{provider.workdir}/timeout-command-finished"
        started = time.monotonic()
        timed_out = provider.execute(f"sleep 5; touch {shlex.quote(timeout_marker)}", timeout=1)
        assert time.monotonic() - started < 8
        assert timed_out.exit_code == 124
        assert "timed out" in timed_out.output
        assert provider.execute("printf 'usable-after-timeout'").output == "usable-after-timeout"
        time.sleep(5.0)
        not_leaked = provider.execute(f"test ! -e {shlex.quote(timeout_marker)}")
        assert not_leaked.exit_code == 0, "timed-out command kept running inside the Sprite"

        blocked = provider.execute(_tcp_probe("1.1.1.1"))
        assert blocked.exit_code != 0


def test_open_network_reaches_public_endpoint() -> None:
    provider = _provider(network="open")
    with _managed(provider):
        reachable = provider.execute(_tcp_probe("1.1.1.1"))
        assert reachable.exit_code == 0, reachable.output
        assert "connected" in reachable.output


def test_allowlist_allows_named_host_and_blocks_other_egress() -> None:
    provider = _provider(network="allowlist", network_allow=("example.com",))
    with _managed(provider):
        allowed = provider.execute(_tcp_probe("example.com"))
        assert allowed.exit_code == 0, allowed.output
        denied = provider.execute(_tcp_probe("1.1.1.1"))
        assert denied.exit_code != 0


def test_package_bootstrap_precedes_blocked_policy() -> None:
    provider = _provider(
        settings=_settings(python_packages=("pyfiglet==1.0.2",)),
    )
    with _managed(provider):
        imported = provider.execute(
            'python3 -c "import importlib.metadata; '
            "assert importlib.metadata.version('pyfiglet') == '1.0.2'; print('bootstrap-ok')\""
        )
        assert imported.exit_code == 0, imported.output
        assert "bootstrap-ok" in imported.output
        assert provider.execute(_tcp_probe("1.1.1.1")).exit_code != 0


def test_bootstrap_failure_deletes_the_created_sprite() -> None:
    provider = _provider(
        settings=_settings(python_packages=("definitely-not-a-real-aiq-sprites-package==0.0.0",)),
    )
    with pytest.raises(SpriteBootstrapError):
        provider.execute("true")
    _assert_absent(provider.sandbox_name)


def test_matching_and_mismatched_name_collisions_are_safe() -> None:
    matching_job = f"live-matching-collision-{uuid4()}"
    matching = _provider(job_id=matching_job)
    with SpritesClient(token=_token()) as client:
        client.create_sprite(
            matching.sandbox_name,
            labels=["aiq-sandbox", _ownership_label(matching_job)],
            runtime="dev",
        )
    with _managed(matching):
        result = matching.execute("printf 'attached-to-owned-sprite'")
        assert result.exit_code == 0
        assert result.output == "attached-to-owned-sprite"

    mismatched = _provider(job_id=f"live-mismatched-collision-{uuid4()}")
    with SpritesClient(token=_token()) as client:
        client.create_sprite(mismatched.sandbox_name, labels=["manual-collision"], runtime="dev")
        try:
            with pytest.raises(SpriteCreationError, match="ownership labels"):
                mismatched.execute("true")
            # The provider must leave an object it does not own untouched.
            assert client.get_sprite(mismatched.sandbox_name).name == mismatched.sandbox_name
        finally:
            client.delete_sprite(mismatched.sandbox_name)
    _assert_absent(mismatched.sandbox_name)


def test_terminate_interrupts_inflight_execution_and_prevents_reuse() -> None:
    provider = _provider()
    assert provider.execute("printf ready").output == "ready"
    outcomes: list[object] = []

    def _execute_long_command() -> None:
        try:
            outcomes.append(provider.execute("sleep 45; printf unexpected", timeout=45))
        except Exception as exc:  # noqa: BLE001 - either a transport error or normalized result is acceptable
            outcomes.append(exc)

    worker = threading.Thread(target=_execute_long_command, daemon=True)
    worker.start()
    time.sleep(1.0)
    started = time.monotonic()
    provider.terminate()
    assert time.monotonic() - started < 10
    worker.join(timeout=15)
    assert not worker.is_alive(), "in-flight execute did not stop after Sprite termination"
    assert outcomes
    outcome = outcomes[0]
    if not isinstance(outcome, Exception):
        assert outcome.exit_code != 0  # type: ignore[attr-defined]
    with pytest.raises(SandboxTerminatedError):
        provider.execute("true")
    _assert_absent(provider.sandbox_name)
