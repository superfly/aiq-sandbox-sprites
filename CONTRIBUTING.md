# Contributing

Contributions are welcome. Open a focused pull request against `main`, keep
credentials and environment-specific data out of commits, and add tests for
behavior changes.

## Development setup

Clone NVIDIA AI-Q beside this repository or into `.aiq-source`, then install the
development dependencies:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Run the local gates with the AI-Q source directory selected:

```bash
AIQ_SOURCE_DIR=/path/to/aiq/src .venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/ruff check --select S src
.venv/bin/python -m build
.venv/bin/twine check dist/*
```

Live tests create and destroy resources in the selected Sprites organization.
Use a restricted test token and opt in explicitly:

```bash
AIQ_SOURCE_DIR=/path/to/aiq/src \
AIQ_SPRITES_LIVE_TEST=1 \
SPRITE_TOKEN=... \
.venv/bin/python -m pytest -vv tests/test_live.py
```

Do not include the token in logs, test output, issues, or pull requests.
