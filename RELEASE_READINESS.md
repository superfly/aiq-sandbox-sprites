# Release readiness: 0.1.0

Date: 2026-08-31

Status: ready for an alpha release and NVIDIA AI-Q review.

## Release artifacts

- `dist/aiq_sandbox_sprites-0.1.0-py3-none-any.whl`
  - SHA-256: `1e42f23afd8021c456b04538605d71c007159d3e6d8d60541e3a9a4ddd5713d7`
- `dist/aiq_sandbox_sprites-0.1.0.tar.gz`
  - SHA-256: `cdaa7715cd2ec0251020ac2a82df6e259afa9c40801e287e1249355e9fc49bad`

The wheel metadata declares Python 3.11 through 3.13, DeepAgents 0.6.8 through
0.7.x, and sprites-py 0.5.x. The source archive contains the examples, runtime
test scripts, source, and tests.

## Validation evidence

### Package gates

```text
ruff check .                         All checks passed
ruff format --check .                13 files already formatted
ruff check --select S src            All checks passed
pytest -q                            37 passed, 7 skipped
python -m build                      wheel and sdist built successfully
python -m twine check dist/*         wheel and sdist passed
```

The offline suite passed on Python 3.11 and Python 3.12. AI-Q runtime tests ran
on Python 3.13. The provider also passes AI-Q's documented sandbox-provider
compliance contract.

### Live Sprites matrix

```text
pytest -vv tests/test_live.py         7 passed
```

The live matrix covers blocked, open, and allowlisted networking; binary file
transfer and byte caps; bounded output; remote command timeouts with process
group termination; trusted package bootstrap; ownership-safe collision
handling; interrupted execution; and cleanup. The Sprites test organization was
empty after the final run.

### AI-Q compatibility

The exact built wheel passed dependency checks and both normal and interrupted
artifact/lifecycle scenarios in these AI-Q environments:

| AI-Q baseline | DeepAgents | sprites-py | Result |
| --- | --- | --- | --- |
| `v2.2.1` (`6ec77d9`) | 0.6.8 | 0.5.1 | compatible; both end-to-end scenarios passed |
| `develop` (`bf4e67d`) | 0.7.7 | 0.5.1 | compatible; both end-to-end scenarios passed |

The end-to-end test verifies checkpoint and final artifact capture, SQLite
artifact bytes, report-reference rewriting, sanitized lifecycle events, busy
harvest behavior, normal cleanup, interrupted finalization, and absence of the
host Sprites credential inside the sandbox.

## Defects found and fixed during manual release testing

- SDK timeout exceptions were not normalized into sandbox results.
- Timed-out commands continued running remotely; the provider now terminates
  the complete remote process group and returns exit code 124.
- Transient package bootstrap failures had no retry.
- Package bootstrap errors could expose installer output; errors are now
  sanitized.
- API URL and numeric settings accepted unsafe or non-finite values.
- The initial DeepAgents lower bound excluded AI-Q 2.2 RC6.
- Newer Hatchling releases emitted Core Metadata 2.5, which current Twine
  rejects; the build backend is pinned to the validated 1.27 line.

## Known alpha limitations

- AI-Q 2.1 lacks the third-party sandbox-provider extension point and is not
  supported.
- CPU and memory limits are deliberately reported as unsupported, so AI-Q fails
  closed when a workflow requests them.
- Provider cleanup cannot run after complete worker-host loss. Production use
  requires an external reaper scoped to provider ownership labels and an
  authoritative job or expiry record.
- The external package currently uses environment variables for
  Sprites-specific configuration because AI-Q does not expose third-party
  provider-specific fields in its YAML model.
