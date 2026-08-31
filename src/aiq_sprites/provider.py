"""AI-Q provider that creates one policy-bound Sprite per research job."""

from __future__ import annotations

import hashlib
import logging
import re
import shlex
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiq_agent.agents.deep_researcher.sandbox.base import SandboxProvider
from aiq_agent.agents.deep_researcher.sandbox.capabilities import SandboxCapabilities
from aiq_agent.agents.deep_researcher.sandbox.registry import register_sandbox_provider
from deepagents.backends.sandbox import BaseSandbox
from sprites import NetworkPolicy, NotFoundError, PolicyRule, Sprite, SpriteError, SpritesClient

from .sandbox import SpriteSandbox
from .settings import SpritesSettings

if TYPE_CHECKING:
    from aiq_agent.agents.deep_researcher.sandbox.config import SandboxConfig

logger = logging.getLogger(__name__)

_PROVIDER_LABEL = "aiq-sandbox"
_NETWORK_REACHABLE = b"AIQ_SPRITES_NETWORK_REACHABLE"
_NETWORK_BLOCKED = b"AIQ_SPRITES_NETWORK_BLOCKED"
_BOOTSTRAP_ATTEMPTS = 2
_PINNED_SPEC = re.compile(r"[A-Za-z0-9._-]+==[A-Za-z0-9._+!-]+")
# Commands run through a login shell, whose PATH an attached Sprite's earlier
# generated code could have prepended to. Pin the lookup to trusted directories.
_PINNED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class SpriteCreationError(RuntimeError):
    """Raised when a job-scoped Sprite cannot be safely created or attached."""


class SpriteBootstrapError(RuntimeError):
    """Raised when trusted dependency bootstrap fails before generated code runs."""


class NetworkVerificationError(RuntimeError):
    """Raised when the requested network policy cannot be attested by a live probe."""


def _job_digest(job_id: str) -> str:
    return hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:24]


def _ownership_label(job_id: str) -> str:
    return f"aiq-job-{_job_digest(job_id)}"


def _installed_check_command(packages: tuple[str, ...]) -> str | None:
    """A command exiting 0 only when every configured package is already installed.

    Returns None when any spec is not a simple `name==version` pin, because nothing
    else can be confirmed without resolving it, and an unconfirmable spec has to be
    installed rather than assumed present.
    """
    if not all(_PINNED_SPEC.fullmatch(package) for package in packages):
        return None
    script = (
        "import importlib.metadata as m, sys\n"
        "for spec in sys.argv[1:]:\n"
        "    name, _, want = spec.partition('==')\n"
        "    if m.version(name) != want:\n"
        "        raise SystemExit(1)\n"
    )
    args = " ".join(shlex.quote(package) for package in packages)
    return f"PATH={_PINNED_PATH}; python3 -c {shlex.quote(script)} {args}"


def _response_detail(response: object) -> str:
    try:
        return str(getattr(response, "text", ""))[:2048]
    except Exception:  # noqa: BLE001 - an error response body is best-effort diagnostics
        return ""


def _policy_rules(mode: str, allow: tuple[str, ...]) -> list[PolicyRule]:
    if mode == "open":
        return []
    if mode == "blocked":
        return [PolicyRule(domain="*", action="deny")]
    if mode == "allowlist":
        return [PolicyRule(domain=host, action="allow") for host in allow]
    raise ValueError(f"Unsupported AI-Q network policy mode: {mode}")


def _rule_tuples(policy: NetworkPolicy) -> list[tuple[str | None, str | None, str | None]]:
    return [(rule.domain, rule.action, rule.include) for rule in policy.rules]


class SpritesSandboxProvider(SandboxProvider):
    """Job-scoped AI-Q provider using a dedicated Firecracker-backed Sprite."""

    provider_name = "sprites"

    def __init__(
        self,
        config: SandboxConfig,
        job_id: str,
        *,
        settings: SpritesSettings | None = None,
        client_factory: Callable[..., SpritesClient] = SpritesClient,
    ) -> None:
        super().__init__(config, job_id)
        self.settings = settings or SpritesSettings.from_env()
        self._client_factory = client_factory

    @classmethod
    def _scoped_name(cls, job_id: str) -> str:
        """Hash the job ID so names are legal, bounded, deterministic, and non-sensitive."""
        return f"aiq-{_job_digest(job_id)}"

    @property
    def capabilities(self) -> SandboxCapabilities:
        """Declare only guarantees implemented and verified by this provider."""
        return SandboxCapabilities(
            supports_network_policy=True,
            supports_network_allowlist=True,
            supports_resource_limits=False,
            supports_artifact_download=True,
            supports_cleanup=True,
            supports_terminate=True,
        )

    def is_recoverable_error(self, exc: Exception) -> bool:
        """Only a typed missing-Sprite error is safe for idempotent recreation."""
        return isinstance(exc, NotFoundError)

    def _create_session(self) -> BaseSandbox:
        """Create first, attach only to the exact marked job Sprite, and fail closed."""
        client = self._client_factory(
            token=self.settings.token,
            base_url=self.settings.base_url,
            timeout=self.settings.api_timeout_seconds,
        )
        session: SpriteSandbox | None = None
        # Bound before the try so the failure handler can never raise NameError when
        # the create-or-attach call itself fails; True means "nothing attached to lose".
        created = True
        try:
            sprite, created = self._create_or_attach(client)
            ownership_label = _ownership_label(self.job_id)
            max_download_bytes = min(
                self.settings.max_download_bytes,
                self.config.artifact_capture.max_file_bytes,
            )
            session = SpriteSandbox(
                sprite=sprite,
                client=client,
                ownership_label=ownership_label,
                max_output_bytes=self.settings.max_output_bytes,
                max_download_bytes=max_download_bytes,
                api_timeout_seconds=self.settings.api_timeout_seconds,
            )

            # A fresh Sprite starts permissive. Only trusted provider bootstrap runs
            # before the requested final policy is applied and live-verified.
            #
            # An attached Sprite is checked instead. Cleanup after a failed creation is
            # best-effort, so an ownership label proves the Sprite is ours but never that
            # it finished bootstrap; the check is what tells the two apart.
            if created:
                self._bootstrap_packages(session)
            else:
                self._bootstrap_attached_packages(session)
            self._configure_network(sprite, had_open_baseline=created)
            return session
        except Exception:
            if session is None:
                client.close()
            else:
                if not created:
                    # An attached Sprite carries earlier work from this same job, and the
                    # close() below destroys it along with the Sprite. Say so.
                    logger.warning(
                        "Destroying attached Sprite %s after a failed session start; "
                        "any earlier in-sandbox work for this job is discarded",
                        self.sandbox_name,
                    )
                try:
                    session.close()
                except Exception:  # noqa: BLE001 - preserve the creation failure, but report cleanup failure
                    logger.exception("Failed to clean up Sprite after provider initialization error")
                    # close() leaves the client open so a delete can be retried. This path
                    # is giving up, so it owns the release rather than leaking a connection.
                    client.close()
            raise

    def _create_or_attach(self, client: SpritesClient) -> tuple[Sprite, bool]:
        ownership_label = _ownership_label(self.job_id)
        labels = [_PROVIDER_LABEL, ownership_label]
        request: dict[str, object] = {"name": self.sandbox_name, "labels": labels}
        if self.settings.runtime is not None:
            request["runtime"] = self.settings.runtime

        response = client.http_client.post(
            f"{client.base_url}/v1/sprites",
            json=request,
            headers={"Content-Type": "application/json"},
            timeout=120.0,
        )
        if response.status_code == 409:
            existing = client.get_sprite(self.sandbox_name)
            if _PROVIDER_LABEL not in existing.labels or ownership_label not in existing.labels:
                raise SpriteCreationError(
                    f"Refusing to attach to existing Sprite {self.sandbox_name!r}: ownership labels do not match"
                )
            logger.info("Attached to existing Sprite for AI-Q job: name=%s", self.sandbox_name)
            return existing, False

        if not response.is_success:
            detail = _response_detail(response)
            suffix = f": {detail}" if detail else ""
            raise SpriteCreationError(f"Failed to create Sprite (status {response.status_code}){suffix}")

        try:
            sprite = client.get_sprite(self.sandbox_name)
            if _PROVIDER_LABEL not in sprite.labels or ownership_label not in sprite.labels:
                raise SpriteCreationError("Created Sprite did not retain the required ownership labels")
        except Exception:
            # The create call succeeded, so this exact name is ours to clean up even
            # if the follow-up ownership read fails. Never leave an untracked Sprite
            # behind on the provider-initialization path.
            try:
                client.delete_sprite(self.sandbox_name)
            except NotFoundError:
                pass
            except Exception:  # noqa: BLE001 - preserve validation failure while recording a potential orphan
                logger.exception("Failed to clean up Sprite after post-create validation error")
            raise
        logger.info("Created Sprite for AI-Q job: name=%s", self.sandbox_name)
        return sprite, True

    def _configured_packages(self) -> tuple[str, ...]:
        configured = self.settings.python_packages
        return tuple(self.config.python_packages) if configured is None else configured

    def _bootstrap_attached_packages(self, session: SpriteSandbox) -> None:
        """Bootstrap a Sprite this provider attached to rather than created.

        The check runs first, and only a Sprite that fails it is installed into. An
        attached Sprite may already carry the restrictive policy, where an install
        cannot reach the index; installing unconditionally would turn a healthy
        resumed sandbox into a failed session and destroy it during cleanup.
        """
        packages = self._configured_packages()
        if not packages:
            return
        check = _installed_check_command(packages)
        if check is not None:
            timeout = min(self.config.timeout, self.settings.bootstrap_timeout_seconds)
            if session.execute(check, timeout=timeout).exit_code == 0:
                logger.info("Attached Sprite already carries its configured packages: name=%s", self.sandbox_name)
                return
        self._bootstrap_packages(session)

    def _bootstrap_packages(self, session: SpriteSandbox) -> None:
        packages = self._configured_packages()
        if not packages:
            return

        package_args = " ".join(shlex.quote(package) for package in packages)
        command = (
            f"PATH={_PINNED_PATH}; "
            "if command -v uv >/dev/null 2>&1; then "
            f"uv pip install --system -- {package_args}; "
            "else "
            f"python3 -m pip install -- {package_args}; "
            "fi"
        )
        timeout = min(self.config.timeout, self.settings.bootstrap_timeout_seconds)
        deadline = time.monotonic() + timeout
        result = None
        attempts = 0
        while attempts < _BOOTSTRAP_ATTEMPTS:
            remaining = int(deadline - time.monotonic())
            if remaining < 1:
                break
            attempts += 1
            result = session.execute(command, timeout=remaining)
            if result.exit_code == 0:
                return
            if attempts < _BOOTSTRAP_ATTEMPTS and deadline - time.monotonic() > 1:
                time.sleep(1.0)

        if result is None:
            raise SpriteBootstrapError(f"Sprite Python package bootstrap exceeded its {timeout}s deadline")
        raise SpriteBootstrapError(
            f"Sprite Python package bootstrap failed after {attempts} attempt(s) (exit {result.exit_code})"
        )

    def _configure_network(self, sprite: Sprite, *, had_open_baseline: bool) -> None:
        mode = self.config.network.mode
        requested = NetworkPolicy(rules=_policy_rules(mode, self.config.network.allow))

        if self.settings.verify_network and mode != "open" and had_open_baseline:
            if not self._probe_egress(sprite, expect_blocked=False):
                raise NetworkVerificationError(
                    "Could not establish the pre-policy network baseline; refusing an unverifiable sandbox"
                )

        sprite.update_network_policy(requested)
        observed = sprite.get_network_policy()
        if _rule_tuples(observed) != _rule_tuples(requested):
            raise NetworkVerificationError("Sprite returned a network policy different from the requested policy")

        if not self.settings.verify_network or mode == "open":
            return

        deadline = time.monotonic() + self.settings.network_verify_timeout_seconds
        while time.monotonic() < deadline:
            if self._probe_egress(sprite, expect_blocked=True):
                return
            time.sleep(0.25)
        raise NetworkVerificationError(
            f"Sprite did not enforce network.mode={mode!r} within {self.settings.network_verify_timeout_seconds:g}s"
        )

    def _probe_egress(self, sprite: Sprite, *, expect_blocked: bool) -> bool:
        if expect_blocked:
            marker = _NETWORK_BLOCKED
            script = """
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
try:
    connection = socket.create_connection((host, port), timeout=2.0)
    connection.close()
except OSError:
    print("AIQ_SPRITES_NETWORK_BLOCKED")
    raise SystemExit(0)
print("AIQ_SPRITES_NETWORK_OPEN")
raise SystemExit(2)
""".strip()
        else:
            marker = _NETWORK_REACHABLE
            script = """
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
try:
    connection = socket.create_connection((host, port), timeout=3.0)
    connection.close()
except OSError as exc:
    print(type(exc).__name__)
    raise SystemExit(2)
print("AIQ_SPRITES_NETWORK_REACHABLE")
""".strip()

        try:
            result = sprite.run(
                "python3",
                "-c",
                script,
                self.settings.deny_probe_host,
                str(self.settings.deny_probe_port),
                capture_output=True,
                timeout=5.0,
            )
        except SpriteError:
            return False
        return result.returncode == 0 and marker in (result.stdout or b"")


register_sandbox_provider("sprites", SpritesSandboxProvider)
