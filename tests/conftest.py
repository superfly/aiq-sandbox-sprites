"""Load AI-Q's real sandbox modules without importing unrelated agent stacks."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType


def pytest_configure() -> None:
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
