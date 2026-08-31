from __future__ import annotations

from importlib.metadata import entry_points
from unittest.mock import MagicMock

import pytest
from aiq_agent.agents.deep_researcher.sandbox.capabilities import CapabilityError, SandboxCapabilities
from aiq_agent.agents.deep_researcher.sandbox.config import SandboxConfig
from aiq_agent.agents.deep_researcher.sandbox.registry import create_sandbox_backend

from aiq_sprites.provider import SpriteBootstrapError, SpriteCreationError, SpritesSandboxProvider
from aiq_sprites.settings import SpritesSettings
from tests.fakes import FakeClient, FakeSprite, completed


def config(**overrides: object) -> SandboxConfig:
    values: dict[str, object] = {
        "provider": "sprites",
        "workdir": "/workspace",
        "network": {"mode": "blocked"},
        "artifact_capture": {"enabled": True, "max_file_bytes": 1024},
    }
    values.update(overrides)
    return SandboxConfig.model_validate(values)


def settings(**overrides: object) -> SpritesSettings:
    values: dict[str, object] = {
        "token": "host-token",
        "base_url": "https://api.sprites.test",
        "verify_network": True,
        "network_verify_timeout_seconds": 1.0,
        "python_packages": (),
    }
    values.update(overrides)
    return SpritesSettings(**values)  # type: ignore[arg-type]


def provider_with_client(
    fake_client: FakeClient,
    *,
    job_id: str = "job/with sensitive id",
    provider_config: SandboxConfig | None = None,
    provider_settings: SpritesSettings | None = None,
) -> SpritesSandboxProvider:
    return SpritesSandboxProvider(
        provider_config or config(),
        job_id,
        settings=provider_settings or settings(),
        client_factory=lambda **kwargs: fake_client,  # type: ignore[arg-type,return-value]
    )


def test_entry_point_is_packaged() -> None:
    matches = {entry.name: entry.value for entry in entry_points(group="aiq.sandbox_providers")}
    assert matches["sprites"] == "aiq_sprites.provider:SpritesSandboxProvider"


def test_names_are_deterministic_bounded_and_do_not_leak_job_id() -> None:
    first = SpritesSandboxProvider._scoped_name("customer/email@example.com")
    second = SpritesSandboxProvider._scoped_name("customer/email@example.com")
    assert first == second
    assert first.startswith("aiq-")
    assert len(first) <= 63
    assert "customer" not in first


def test_fresh_session_bootstraps_then_applies_and_verifies_blocked_policy() -> None:
    sprite = FakeSprite("placeholder", [])
    client = FakeClient(sprite=sprite)
    provider = provider_with_client(client)
    sprite.name = provider.sandbox_name
    sprite.labels = ["aiq-sandbox", f"aiq-job-{provider.sandbox_name.removeprefix('aiq-')}"]
    sprite.run_results.extend(
        [
            completed(0, b"AIQ_SPRITES_NETWORK_REACHABLE\n"),
            completed(0, b"AIQ_SPRITES_NETWORK_BLOCKED\n"),
        ]
    )

    session = provider._create_session()

    assert session.id == provider.sandbox_name
    request = client.create_requests[0]["json"]
    assert request["name"] == provider.sandbox_name
    assert request["runtime"] == "dev"
    assert "host-token" not in repr(request)
    assert [(rule.domain, rule.action) for rule in sprite.network_policy.rules] == [("*", "deny")]
    session.close()
    assert client.deleted == [provider.sandbox_name]


def test_bootstrap_uses_trusted_argv_before_network_policy() -> None:
    sprite = FakeSprite("placeholder", [])
    client = FakeClient(sprite=sprite)
    provider = provider_with_client(client, provider_settings=settings(python_packages=("numpy==2.3.0",)))
    sprite.name = provider.sandbox_name
    sprite.labels = ["aiq-sandbox", f"aiq-job-{provider.sandbox_name.removeprefix('aiq-')}"]
    sprite.command_results.append((b"installed", 0, False))
    sprite.run_results.extend(
        [
            completed(0, b"AIQ_SPRITES_NETWORK_REACHABLE\n"),
            completed(0, b"AIQ_SPRITES_NETWORK_BLOCKED\n"),
        ]
    )

    session = provider._create_session()

    bootstrap = sprite.commands[0]["env"]["AIQ_SPRITES_COMMAND"]
    assert "uv pip install --system" in bootstrap
    assert "numpy==2.3.0" in bootstrap
    session.close()


def test_bootstrap_retries_one_fast_failure_within_the_shared_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiq_sprites.provider.time.sleep", lambda _: None)
    sprite = FakeSprite("placeholder", [])
    client = FakeClient(sprite=sprite)
    provider = provider_with_client(client, provider_settings=settings(python_packages=("numpy==2.3.0",)))
    sprite.name = provider.sandbox_name
    sprite.labels = ["aiq-sandbox", f"aiq-job-{provider.sandbox_name.removeprefix('aiq-')}"]
    sprite.command_results.extend([(b"temporary index error", 1, False), (b"installed", 0, False)])
    sprite.run_results.extend(
        [
            completed(0, b"AIQ_SPRITES_NETWORK_REACHABLE\n"),
            completed(0, b"AIQ_SPRITES_NETWORK_BLOCKED\n"),
        ]
    )

    session = provider._create_session()

    assert len(sprite.commands) == 2
    assert sprite.commands[0]["env"] == sprite.commands[1]["env"]
    session.close()


def test_bootstrap_failure_redacts_installer_output_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiq_sprites.provider.time.sleep", lambda _: None)
    sprite = FakeSprite("placeholder", [])
    client = FakeClient(sprite=sprite)
    package = "private-package @ https://user:credential@example.test/package.whl"
    provider = provider_with_client(client, provider_settings=settings(python_packages=(package,)))
    sprite.name = provider.sandbox_name
    sprite.labels = ["aiq-sandbox", f"aiq-job-{provider.sandbox_name.removeprefix('aiq-')}"]
    sprite.command_results.extend(
        [
            (b"download failed for https://user:credential@example.test/package.whl", 1, False),
            (b"credential=do-not-expose", 1, False),
        ]
    )

    with pytest.raises(SpriteBootstrapError) as captured:
        provider._create_session()

    message = str(captured.value)
    assert "credential" not in message
    assert "example.test" not in message
    assert client.deleted == [provider.sandbox_name]
    assert client.closed is True


def test_matching_collision_attaches_but_mismatched_collision_fails_closed() -> None:
    matching_sprite = FakeSprite("placeholder", [])
    matching_client = FakeClient(sprite=matching_sprite, create_status=409)
    provider = provider_with_client(matching_client)
    matching_sprite.name = provider.sandbox_name
    matching_sprite.labels = ["aiq-sandbox", f"aiq-job-{provider.sandbox_name.removeprefix('aiq-')}"]
    matching_sprite.run_results.append(completed(0, b"AIQ_SPRITES_NETWORK_BLOCKED\n"))

    attached = provider._create_session()
    assert attached.id == provider.sandbox_name
    attached.close()

    wrong_sprite = FakeSprite(provider.sandbox_name, ["aiq-sandbox", "aiq-job-wrong"])
    wrong_client = FakeClient(sprite=wrong_sprite, create_status=409)
    wrong_provider = provider_with_client(wrong_client)
    with pytest.raises(SpriteCreationError, match="ownership labels"):
        wrong_provider._create_session()
    assert wrong_client.deleted == []
    assert wrong_client.closed is True


def test_post_create_validation_failure_cleans_up_created_name() -> None:
    sprite = FakeSprite("placeholder", ["labels-were-lost"])
    client = FakeClient(sprite=sprite)
    provider = provider_with_client(client)
    sprite.name = provider.sandbox_name

    with pytest.raises(SpriteCreationError, match="ownership labels"):
        provider._create_session()

    assert client.deleted == [provider.sandbox_name]
    assert client.closed is True


def test_capabilities_fail_closed_on_requested_resource_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPRITE_TOKEN", "host-token")
    with pytest.raises(CapabilityError, match="resource limits"):
        create_sandbox_backend(config(resources={"cpu": 2}), "job")


def test_aiq_base_lifecycle_executes_and_cleans_up() -> None:
    sprite = FakeSprite("placeholder", [])
    client = FakeClient(sprite=sprite)
    provider = provider_with_client(client, provider_settings=settings(verify_network=False))
    sprite.name = provider.sandbox_name
    sprite.labels = ["aiq-sandbox", f"aiq-job-{provider.sandbox_name.removeprefix('aiq-')}"]
    # AI-Q prepares workspace first, then executes the requested command.
    sprite.command_results.extend([(b"", 0, False), (b"contract-ok\n", 0, False)])

    result = provider.execute("printf contract-ok")
    provider.terminate()

    assert result.output == "contract-ok\n"
    assert result.exit_code == 0
    assert client.deleted == [provider.sandbox_name]


def test_aiq_provider_compliance_contract() -> None:
    """Mirror AI-Q's provider compliance harness for this out-of-tree provider."""
    provider = provider_with_client(FakeClient(sprite=FakeSprite("placeholder", [])))

    assert isinstance(provider.capabilities, SandboxCapabilities)
    assert isinstance(provider.sandbox_name, str) and provider.sandbox_name
    assert provider.id == provider.sandbox_name
    assert provider.is_recoverable_error(ValueError("unrelated")) is False

    provider.close()
    provider.close()

    session = MagicMock()
    session.execute.return_value = "ok"
    provider._create_session = lambda: session  # type: ignore[method-assign]

    assert provider.execute("echo ok", timeout=5) == "ok"
    first_cmd = session.execute.call_args_list[0].args[0]
    assert first_cmd.startswith("mkdir -p") and provider.workdir in first_cmd
    session.execute.assert_called_with("echo ok", timeout=5)
