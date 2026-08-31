from __future__ import annotations

import logging
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


class _DeleteFailsClient(FakeClient):
    """A client whose Sprite deletion fails transiently, leaving the Sprite in place."""

    def delete_sprite(self, name: str) -> None:
        self.deleted.append(name)
        raise RuntimeError("transient delete failure")


def test_attach_bootstraps_a_sprite_left_behind_by_failed_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiq_sprites.provider.time.sleep", lambda _: None)
    packages = settings(python_packages=("numpy==2.3.0",))

    sprite = FakeSprite("placeholder", [])
    failing = _DeleteFailsClient(sprite=sprite)
    first = provider_with_client(failing, provider_settings=packages)
    sprite.name = first.sandbox_name
    sprite.labels = ["aiq-sandbox", f"aiq-job-{first.sandbox_name.removeprefix('aiq-')}"]
    sprite.command_results.extend([(b"install failed", 1, False), (b"install failed", 1, False)])

    with pytest.raises(SpriteBootstrapError):
        first._create_session()

    # Cleanup was attempted and failed, so a labelled but un-bootstrapped Sprite survives.
    assert failing.deleted == [first.sandbox_name]
    assert failing.sprite is sprite
    already_run = len(sprite.commands)

    retry = FakeClient(sprite=sprite, create_status=409)
    second = provider_with_client(retry, provider_settings=packages)
    # The survivor never finished bootstrap, so the installed-check fails and the
    # install that follows succeeds.
    sprite.command_results.append((b"", 1, False))
    sprite.run_results.append(completed(0, b"AIQ_SPRITES_NETWORK_BLOCKED\n"))
    session = second._create_session()

    installs = [
        command for command in sprite.commands[already_run:] if "install" in command["env"]["AIQ_SPRITES_COMMAND"]
    ]
    assert installs, "attached Sprite was handed to the job without its configured python_packages"
    session.close()


def test_attach_fails_closed_when_the_left_behind_sprite_cannot_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiq_sprites.provider.time.sleep", lambda _: None)
    packages = settings(python_packages=("numpy==2.3.0",))

    sprite = FakeSprite("placeholder", [])
    retry = FakeClient(sprite=sprite, create_status=409)
    provider = provider_with_client(retry, provider_settings=packages)
    sprite.name = provider.sandbox_name
    sprite.labels = ["aiq-sandbox", f"aiq-job-{provider.sandbox_name.removeprefix('aiq-')}"]
    # An orphan left behind after its policy was applied fails the check and can no
    # longer reach the index to repair itself.
    sprite.command_results.extend(
        [(b"", 1, False), (b"network is unreachable", 1, False), (b"network is unreachable", 1, False)]
    )
    # Present so that code which skips bootstrap entirely reaches the policy probe and
    # returns a session, making this test fail on its own assertion rather than on an
    # unexpected-probe error from the fake.
    sprite.run_results.append(completed(0, b"AIQ_SPRITES_NETWORK_BLOCKED\n"))

    with pytest.raises(SpriteBootstrapError):
        provider._create_session()

    # Fail closed, and clear the orphan so the next attempt creates a fresh Sprite.
    assert retry.deleted == [provider.sandbox_name]


def test_attach_leaves_an_already_bootstrapped_sprite_alone() -> None:
    packages = settings(python_packages=("numpy==2.3.0",))

    sprite = FakeSprite("placeholder", [])
    client = FakeClient(sprite=sprite, create_status=409)
    provider = provider_with_client(client, provider_settings=packages)
    sprite.name = provider.sandbox_name
    sprite.labels = ["aiq-sandbox", f"aiq-job-{provider.sandbox_name.removeprefix('aiq-')}"]
    # The packages are already there, so the check passes and no install is attempted.
    # A restricted Sprite could not reach the index if one were.
    sprite.command_results.append((b"", 0, False))
    sprite.run_results.append(completed(0, b"AIQ_SPRITES_NETWORK_BLOCKED\n"))

    session = provider._create_session()

    assert session.id == provider.sandbox_name
    assert client.deleted == []
    installs = [c for c in sprite.commands if "install" in c["env"]["AIQ_SPRITES_COMMAND"]]
    assert not installs, "reinstalled packages that were already present on the attached Sprite"


_PINNED_PATH_PREFIX = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def test_bootstrap_install_pins_the_installer_lookup_path() -> None:
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
    assert bootstrap.startswith(_PINNED_PATH_PREFIX)
    assert "command -v uv" in bootstrap
    session.close()


def test_attached_installed_check_pins_its_lookup_path() -> None:
    sprite = FakeSprite("placeholder", [])
    client = FakeClient(sprite=sprite, create_status=409)
    provider = provider_with_client(client, provider_settings=settings(python_packages=("numpy==2.3.0",)))
    sprite.name = provider.sandbox_name
    sprite.labels = ["aiq-sandbox", f"aiq-job-{provider.sandbox_name.removeprefix('aiq-')}"]
    sprite.command_results.append((b"", 0, False))
    sprite.run_results.append(completed(0, b"AIQ_SPRITES_NETWORK_BLOCKED\n"))

    session = provider._create_session()

    check = sprite.commands[0]["env"]["AIQ_SPRITES_COMMAND"]
    assert check.startswith(_PINNED_PATH_PREFIX)
    session.close()


def test_attach_path_failure_warns_that_the_workspace_is_discarded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("aiq_sprites.provider.time.sleep", lambda _: None)
    packages = settings(python_packages=("numpy==2.3.0",))

    sprite = FakeSprite("placeholder", [])
    client = FakeClient(sprite=sprite, create_status=409)
    provider = provider_with_client(client, provider_settings=packages)
    sprite.name = provider.sandbox_name
    sprite.labels = ["aiq-sandbox", f"aiq-job-{provider.sandbox_name.removeprefix('aiq-')}"]
    sprite.command_results.extend([(b"", 1, False), (b"install failed", 1, False), (b"install failed", 1, False)])

    with caplog.at_level(logging.WARNING, logger="aiq_sprites.provider"):
        with pytest.raises(SpriteBootstrapError):
            provider._create_session()

    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any(provider.sandbox_name in message and "discard" in message.lower() for message in warnings), warnings
    assert client.deleted == [provider.sandbox_name]

    # A Sprite this provider created holds nothing but its own failed bootstrap.
    caplog.clear()
    fresh_sprite = FakeSprite("placeholder", [])
    fresh_client = FakeClient(sprite=fresh_sprite)
    fresh = provider_with_client(fresh_client, provider_settings=packages)
    fresh_sprite.name = fresh.sandbox_name
    fresh_sprite.labels = ["aiq-sandbox", f"aiq-job-{fresh.sandbox_name.removeprefix('aiq-')}"]
    fresh_sprite.command_results.extend([(b"install failed", 1, False), (b"install failed", 1, False)])

    with caplog.at_level(logging.WARNING, logger="aiq_sprites.provider"):
        with pytest.raises(SpriteBootstrapError):
            fresh._create_session()

    assert [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING] == []


def test_failure_before_the_sprite_exists_reports_the_original_error() -> None:
    client = FakeClient(sprite=FakeSprite("placeholder", []))
    provider = provider_with_client(client)

    def explode(_: object) -> tuple[object, bool]:
        raise SpriteCreationError("upstream API refused the create call")

    provider._create_or_attach = explode  # type: ignore[method-assign]

    with pytest.raises(SpriteCreationError, match="upstream API refused"):
        provider._create_session()

    assert client.closed is True


def _installs(sprite: FakeSprite, start: int = 0) -> list[str]:
    commands = [command["env"]["AIQ_SPRITES_COMMAND"] for command in sprite.commands[start:]]
    return [command for command in commands if "pip install" in command]


def _checks(sprite: FakeSprite, start: int = 0) -> list[str]:
    commands = [command["env"]["AIQ_SPRITES_COMMAND"] for command in sprite.commands[start:]]
    return [command for command in commands if "importlib.metadata" in command]


def test_attach_installs_directly_when_a_spec_cannot_be_confirmed() -> None:
    package = "private-pkg @ https://example.test/p.whl"
    sprite = FakeSprite("placeholder", [])
    client = FakeClient(sprite=sprite, create_status=409)
    provider = provider_with_client(client, provider_settings=settings(python_packages=(package,)))
    sprite.name = provider.sandbox_name
    sprite.labels = ["aiq-sandbox", f"aiq-job-{provider.sandbox_name.removeprefix('aiq-')}"]
    sprite.command_results.append((b"installed", 0, False))
    sprite.run_results.append(completed(0, b"AIQ_SPRITES_NETWORK_BLOCKED\n"))

    session = provider._create_session()

    assert _checks(sprite) == [], "ran an installed-check for a spec it cannot confirm"
    assert len(_installs(sprite)) == 1
    session.close()


class _OrderingSprite(FakeSprite):
    """A Sprite recording bootstrap commands and policy updates in arrival order."""

    def __init__(self, name: str, labels: list[str]) -> None:
        super().__init__(name, labels)
        self.events: list[str] = []

    def command(self, *args: object, **kwargs: object) -> object:
        self.events.append("bootstrap")
        return super().command(*args, **kwargs)  # type: ignore[arg-type]

    def update_network_policy(self, policy: object) -> None:
        self.events.append("policy")
        super().update_network_policy(policy)  # type: ignore[arg-type]


def test_attach_bootstraps_before_the_network_policy_is_applied() -> None:
    sprite = _OrderingSprite("placeholder", [])
    client = FakeClient(sprite=sprite, create_status=409)
    provider = provider_with_client(client, provider_settings=settings(python_packages=("numpy==2.3.0",)))
    sprite.name = provider.sandbox_name
    sprite.labels = ["aiq-sandbox", f"aiq-job-{provider.sandbox_name.removeprefix('aiq-')}"]
    # Check fails, install succeeds: both must land while the Sprite can still reach the index.
    sprite.command_results.extend([(b"", 1, False), (b"installed", 0, False)])
    sprite.run_results.append(completed(0, b"AIQ_SPRITES_NETWORK_BLOCKED\n"))

    session = provider._create_session()

    assert sprite.events == ["bootstrap", "bootstrap", "policy"]
    session.close()


def test_a_cleared_orphan_lets_the_next_session_start_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiq_sprites.provider.time.sleep", lambda _: None)
    packages = settings(python_packages=("numpy==2.3.0",))

    orphan = FakeSprite("placeholder", [])
    failing_client = FakeClient(sprite=orphan, create_status=409)
    first = provider_with_client(failing_client, provider_settings=packages)
    orphan.name = first.sandbox_name
    orphan.labels = ["aiq-sandbox", f"aiq-job-{first.sandbox_name.removeprefix('aiq-')}"]
    orphan.command_results.extend(
        [(b"", 1, False), (b"network is unreachable", 1, False), (b"network is unreachable", 1, False)]
    )

    with pytest.raises(SpriteBootstrapError):
        first._create_session()

    # The delete succeeded, so the name is free again.
    assert failing_client.deleted == [first.sandbox_name]
    assert failing_client.sprite is None

    replacement = FakeSprite("placeholder", [])
    client = FakeClient(sprite=replacement)
    second = provider_with_client(client, provider_settings=packages)
    replacement.name = second.sandbox_name
    replacement.labels = ["aiq-sandbox", f"aiq-job-{second.sandbox_name.removeprefix('aiq-')}"]
    replacement.command_results.append((b"installed", 0, False))
    replacement.run_results.extend(
        [
            completed(0, b"AIQ_SPRITES_NETWORK_REACHABLE\n"),
            completed(0, b"AIQ_SPRITES_NETWORK_BLOCKED\n"),
        ]
    )

    session = second._create_session()

    assert client.create_requests, "the next attempt did not try to create a Sprite"
    assert len(_installs(replacement)) == 1
    assert [(rule.domain, rule.action) for rule in replacement.network_policy.rules] == [("*", "deny")]
    session.close()


def test_an_undeletable_orphan_makes_every_retry_pay_for_the_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiq_sprites.provider.time.sleep", lambda _: None)
    packages = settings(python_packages=("numpy==2.3.0",))

    orphan = FakeSprite("placeholder", [])
    client = _DeleteFailsClient(sprite=orphan, create_status=409)
    attempts = 3
    for _ in range(attempts):
        provider = provider_with_client(client, provider_settings=packages)
        orphan.name = provider.sandbox_name
        orphan.labels = ["aiq-sandbox", f"aiq-job-{provider.sandbox_name.removeprefix('aiq-')}"]
        # Check fails, then both install attempts fail against the restricted network.
        orphan.command_results.extend(
            [(b"", 1, False), (b"network is unreachable", 1, False), (b"network is unreachable", 1, False)]
        )
        with pytest.raises(SpriteBootstrapError):
            provider._create_session()

    # The orphan survives every attempt, and every attempt re-pays the full install budget.
    assert client.sprite is orphan
    assert client.deleted == [orphan.name] * attempts
    assert len(_checks(orphan)) == attempts
    assert len(_installs(orphan)) == attempts * 2


def test_a_failed_cleanup_still_releases_the_api_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiq_sprites.provider.time.sleep", lambda _: None)
    sprite = FakeSprite("placeholder", [])
    client = _DeleteFailsClient(sprite=sprite, create_status=409)
    provider = provider_with_client(client, provider_settings=settings(python_packages=("numpy==2.3.0",)))
    sprite.name = provider.sandbox_name
    sprite.labels = ["aiq-sandbox", f"aiq-job-{provider.sandbox_name.removeprefix('aiq-')}"]
    sprite.command_results.extend([(b"", 1, False), (b"no net", 1, False), (b"no net", 1, False)])

    with pytest.raises(SpriteBootstrapError):
        provider._create_session()

    # close() keeps the client open so a delete can be retried. Nothing retries here,
    # so this path owns releasing it rather than leaking a connection per attempt.
    assert client.closed is True
