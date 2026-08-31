# AI-Q sandbox provider for Fly.io Sprites

`aiq-sandbox-sprites` runs NVIDIA AI-Q generated code in one isolated
[Fly.io Sprite](https://sprites.dev) per AI-Q job. It implements AI-Q's official
third-party `aiq.sandbox_providers` entry point and LangChain Deep Agents'
`BaseSandbox` contract.

This package is an alpha proof of concept. It deliberately reports resource
limits as unsupported, so AI-Q fails closed if a workflow requests CPU or
memory limits that Sprites cannot currently enforce through its public API.

## Installation

Install AI-Q from NVIDIA's source distribution, then install the provider into
that same Python environment:

```bash
pip install aiq-sandbox-sprites
```

AI-Q 2.2 is not currently published as an `aiq-agent` PyPI distribution, so the
provider declares the shared Deep Agents protocol rather than an unresolvable
host-package dependency.

The provider requires AI-Q 2.2's `aiq.sandbox_providers` extension point. AI-Q
2.1 does not expose that interface and is not supported. The tested compatibility
range currently covers AI-Q `v2.2.0-rc6` with Deep Agents 0.6.8 and AI-Q's
`develop` branch with Deep Agents 0.7.x.

Set a restricted Sprites token on the AI-Q host:

```bash
export SPRITE_TOKEN="..."
```

The token is used by the host-side SDK. It is never copied into a Sprite or
passed to generated code.

## AI-Q configuration

Select `sprites` in the normal AI-Q sandbox block:

```yaml
functions:
  deep_research_sandbox:
    _type: deep_research_sandbox
    provider: sprites
    workdir: /workspace
    network: blocked
    timeout: 1200
    idle_timeout: 1800
    artifact_capture:
      enabled: true
      max_file_bytes: 50000000
      allow_extensions: [.png, .jpg, .jpeg, .webp, .csv, .json, .md, .ipynb, .pdf]
```

AI-Q 2.2 does not yet expose third-party provider-specific fields in its YAML
model. Configure the temporary Sprites-specific surface through environment
variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SPRITE_TOKEN` | required | Host-side Sprites API token |
| `AIQ_SPRITES_API_URL` | `https://api.sprites.dev` | Sprites API endpoint |
| `AIQ_SPRITES_RUNTIME` | `dev` | Sprite runtime; empty means the API default |
| `AIQ_SPRITES_PYTHON_PACKAGES` | unset | JSON array of locked package requirements installed before network policy |
| `AIQ_SPRITES_MAX_OUTPUT_BYTES` | `1000000` | Maximum combined command output returned to AI-Q |
| `AIQ_SPRITES_MAX_DOWNLOAD_BYTES` | `50000000` | Independent upper bound on one downloaded file |
| `AIQ_SPRITES_VERIFY_NETWORK` | `true` | Require live pre/post-policy connectivity probes |
| `AIQ_SPRITES_NETWORK_VERIFY_TIMEOUT_SECONDS` | `10` | Time allowed for policy enforcement |
| `AIQ_SPRITES_DENY_PROBE_HOST` | `1.1.1.1` | Known-reachable direct-IP egress probe |
| `AIQ_SPRITES_DENY_PROBE_PORT` | `443` | Probe TCP port |

For the AI-Q analytics profile:

```bash
export AIQ_SPRITES_PYTHON_PACKAGES='["matplotlib", "numpy", "pandas", "pillow", "tabulate"]'
```

## Lifecycle and security behavior

- Job IDs are hashed into deterministic `aiq-<digest>` Sprite names and matching
  ownership labels.
- Creation is attempted first. A name collision is attachable only when both
  the provider and exact job ownership labels match.
- Trusted package bootstrap runs only for a newly created Sprite, before
  generated code and before the final network restriction is installed.
- `blocked` maps to a wildcard deny policy; `allowlist` maps to explicit allow
  rules with default deny; `open` maps to an empty policy.
- Restricted policies are checked through the policy API and a live direct-IP
  connectivity probe before the session is returned to AI-Q.
- Command output is drained inside the Sprite while only a bounded head/tail is
  returned, preventing unbounded SDK buffers.
- File downloads are streamed with a byte cap. Oversized files return the
  per-file `file_too_large` error instead of being loaded into host memory.
- Close and terminate re-check the job ownership label before destroying the
  Sprite. Missing Sprites are treated as already cleaned up.

Provider cleanup cannot run if the complete AI-Q worker is lost. A production
deployment still requires an external reaper scoped to the `aiq-sandbox` label
and an authoritative job/expiry record.

## Development

Tests use the current AI-Q sandbox source through `PYTHONPATH` and never create
cloud resources:

```bash
PYTHONPATH=/path/to/aiq/src .venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
```

The opt-in smoke test creates one disposable Sprite, runs commands and file
transfer, verifies blocked egress, terminates the provider, and confirms the
Sprite no longer exists:

```bash
AIQ_SOURCE_DIR=/path/to/aiq/src .venv/bin/python scripts/smoke_test.py
```

The broader live release matrix covers blocked, open, and allowlisted networking;
binary transfer and size bounds; bounded output; command timeout recovery; package
bootstrap; collision ownership; interrupted execution; and cleanup:

```bash
AIQ_SOURCE_DIR=/path/to/aiq/src \
AIQ_SPRITES_LIVE_TEST=1 \
.venv/bin/pytest -vv tests/test_live.py
```

After installing the built wheel into a complete AI-Q environment, exercise the
real AI-Q runtime, artifact store, lifecycle events, and normal/interrupted cleanup:

```bash
python scripts/aiq_runtime_test.py
```
