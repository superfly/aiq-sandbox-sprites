"""Operator-owned configuration for the external AI-Q provider."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

_DEFAULT_BASE_URL = "https://api.sprites.dev"
_DEFAULT_RUNTIME = "dev"
_DEFAULT_MAX_OUTPUT_BYTES = 1_000_000
_DEFAULT_MAX_DOWNLOAD_BYTES = 50_000_000
_DEFAULT_API_TIMEOUT_SECONDS = 30.0
_DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS = 600
_DEFAULT_NETWORK_VERIFY_TIMEOUT_SECONDS = 10.0
_DEFAULT_DENY_PROBE_HOST = "1.1.1.1"
_DEFAULT_DENY_PROBE_PORT = 443


class SettingsError(ValueError):
    """Raised when an AI-Q Sprites environment setting is unsafe or invalid."""


def _read_bool(environment: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environment.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{name} must be a boolean (true/false)")


def _read_int(
    environment: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    raw = environment.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if value < minimum:
        raise SettingsError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise SettingsError(f"{name} must be at most {maximum}")
    return value


def _read_float(
    environment: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
) -> float:
    raw = environment.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < minimum:
        raise SettingsError(f"{name} must be a finite number at least {minimum}")
    return value


def _read_packages(environment: Mapping[str, str]) -> tuple[str, ...] | None:
    raw = environment.get("AIQ_SPRITES_PYTHON_PACKAGES")
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsError("AIQ_SPRITES_PYTHON_PACKAGES must be a JSON array of package requirements") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise SettingsError("AIQ_SPRITES_PYTHON_PACKAGES must be a JSON array of non-empty strings")
    return tuple(item.strip() for item in value)


@dataclass(frozen=True, slots=True)
class SpritesSettings:
    """Settings not representable in AI-Q's provider-neutral sandbox config."""

    token: str
    base_url: str = _DEFAULT_BASE_URL
    runtime: str | None = _DEFAULT_RUNTIME
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES
    max_download_bytes: int = _DEFAULT_MAX_DOWNLOAD_BYTES
    api_timeout_seconds: float = _DEFAULT_API_TIMEOUT_SECONDS
    bootstrap_timeout_seconds: int = _DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS
    verify_network: bool = True
    network_verify_timeout_seconds: float = _DEFAULT_NETWORK_VERIFY_TIMEOUT_SECONDS
    deny_probe_host: str = _DEFAULT_DENY_PROBE_HOST
    deny_probe_port: int = _DEFAULT_DENY_PROBE_PORT
    python_packages: tuple[str, ...] | None = None

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> SpritesSettings:
        """Load settings from environment variables without copying credentials into a Sprite."""
        env = os.environ if environment is None else environment
        token = env.get("SPRITE_TOKEN", "").strip()
        if not token:
            raise SettingsError("SPRITE_TOKEN is required for the AI-Q Sprites sandbox provider")

        base_url = env.get("AIQ_SPRITES_API_URL", _DEFAULT_BASE_URL).strip().rstrip("/")
        parsed_url = urlsplit(base_url)
        if parsed_url.scheme not in {"http", "https"} or parsed_url.hostname is None:
            raise SettingsError("AIQ_SPRITES_API_URL must be an http(s) URL with a hostname")
        if parsed_url.username is not None or parsed_url.password is not None:
            raise SettingsError("AIQ_SPRITES_API_URL must not contain credentials")
        if parsed_url.scheme == "http" and parsed_url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise SettingsError("AIQ_SPRITES_API_URL must use HTTPS except for a loopback development endpoint")

        raw_runtime = env.get("AIQ_SPRITES_RUNTIME", _DEFAULT_RUNTIME).strip()
        runtime = raw_runtime or None
        deny_probe_host = env.get("AIQ_SPRITES_DENY_PROBE_HOST", _DEFAULT_DENY_PROBE_HOST).strip()
        if not deny_probe_host:
            raise SettingsError("AIQ_SPRITES_DENY_PROBE_HOST cannot be empty")

        return cls(
            token=token,
            base_url=base_url,
            runtime=runtime,
            max_output_bytes=_read_int(
                env,
                "AIQ_SPRITES_MAX_OUTPUT_BYTES",
                _DEFAULT_MAX_OUTPUT_BYTES,
                minimum=4096,
            ),
            max_download_bytes=_read_int(
                env,
                "AIQ_SPRITES_MAX_DOWNLOAD_BYTES",
                _DEFAULT_MAX_DOWNLOAD_BYTES,
                minimum=1,
            ),
            api_timeout_seconds=_read_float(
                env,
                "AIQ_SPRITES_API_TIMEOUT_SECONDS",
                _DEFAULT_API_TIMEOUT_SECONDS,
                minimum=0.1,
            ),
            bootstrap_timeout_seconds=_read_int(
                env,
                "AIQ_SPRITES_BOOTSTRAP_TIMEOUT_SECONDS",
                _DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS,
                minimum=1,
            ),
            verify_network=_read_bool(env, "AIQ_SPRITES_VERIFY_NETWORK", True),
            network_verify_timeout_seconds=_read_float(
                env,
                "AIQ_SPRITES_NETWORK_VERIFY_TIMEOUT_SECONDS",
                _DEFAULT_NETWORK_VERIFY_TIMEOUT_SECONDS,
                minimum=1.0,
            ),
            deny_probe_host=deny_probe_host,
            deny_probe_port=_read_int(
                env,
                "AIQ_SPRITES_DENY_PROBE_PORT",
                _DEFAULT_DENY_PROBE_PORT,
                minimum=1,
                maximum=65535,
            ),
            python_packages=_read_packages(env),
        )
