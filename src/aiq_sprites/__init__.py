"""Fly.io Sprites sandbox provider for NVIDIA AI-Q.

The public imports are lazy so metadata and settings can be inspected before the
package is loaded inside an AI-Q environment.
"""

from __future__ import annotations

from typing import Any

__all__ = ["SpriteSandbox", "SpritesSandboxProvider", "SpritesSettings"]


def __getattr__(name: str) -> Any:
    if name == "SpriteSandbox":
        from .sandbox import SpriteSandbox

        return SpriteSandbox
    if name == "SpritesSandboxProvider":
        from .provider import SpritesSandboxProvider

        return SpritesSandboxProvider
    if name == "SpritesSettings":
        from .settings import SpritesSettings

        return SpritesSettings
    raise AttributeError(name)
