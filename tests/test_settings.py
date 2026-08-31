from __future__ import annotations

import pytest

from aiq_sprites.settings import SettingsError, SpritesSettings


def test_settings_require_host_token() -> None:
    with pytest.raises(SettingsError, match="SPRITE_TOKEN"):
        SpritesSettings.from_env({})


def test_settings_parse_bounded_values_and_packages() -> None:
    settings = SpritesSettings.from_env(
        {
            "SPRITE_TOKEN": "secret",
            "AIQ_SPRITES_API_URL": "https://sprites.example/",
            "AIQ_SPRITES_RUNTIME": "",
            "AIQ_SPRITES_VERIFY_NETWORK": "false",
            "AIQ_SPRITES_MAX_OUTPUT_BYTES": "4096",
            "AIQ_SPRITES_PYTHON_PACKAGES": '["numpy==2.3.0", "pandas>=2,<3"]',
        }
    )
    assert settings.token == "secret"
    assert settings.base_url == "https://sprites.example"
    assert settings.runtime is None
    assert settings.verify_network is False
    assert settings.max_output_bytes == 4096
    assert settings.python_packages == ("numpy==2.3.0", "pandas>=2,<3")


def test_settings_allow_cleartext_only_for_loopback_development() -> None:
    settings = SpritesSettings.from_env(
        {
            "SPRITE_TOKEN": "secret",
            "AIQ_SPRITES_API_URL": "http://127.0.0.1:8080/",
        }
    )
    assert settings.base_url == "http://127.0.0.1:8080"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://sprites.example",
        "https://user:credential@sprites.example",
        "https://",
    ],
)
def test_settings_reject_unsafe_or_malformed_api_urls(base_url: str) -> None:
    with pytest.raises(SettingsError):
        SpritesSettings.from_env({"SPRITE_TOKEN": "secret", "AIQ_SPRITES_API_URL": base_url})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AIQ_SPRITES_VERIFY_NETWORK", "maybe"),
        ("AIQ_SPRITES_MAX_OUTPUT_BYTES", "100"),
        ("AIQ_SPRITES_DENY_PROBE_PORT", "0"),
        ("AIQ_SPRITES_DENY_PROBE_PORT", "65536"),
        ("AIQ_SPRITES_API_TIMEOUT_SECONDS", "nan"),
        ("AIQ_SPRITES_NETWORK_VERIFY_TIMEOUT_SECONDS", "inf"),
        ("AIQ_SPRITES_PYTHON_PACKAGES", "numpy"),
    ],
)
def test_settings_reject_invalid_values(name: str, value: str) -> None:
    with pytest.raises(SettingsError):
        SpritesSettings.from_env({"SPRITE_TOKEN": "secret", name: value})
