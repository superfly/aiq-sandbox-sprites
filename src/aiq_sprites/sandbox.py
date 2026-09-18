"""LangChain Deep Agents sandbox session backed by one Fly.io Sprite."""

from __future__ import annotations

import io
import shlex
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx
from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from deepagents.backends.sandbox import BaseSandbox
from sprites import FileNotFoundError_, FilesystemError, IsADirectoryError_, NotFoundError, PermissionError_
from sprites.exceptions import ExecError
from sprites.exceptions import TimeoutError as SpriteTimeoutError

if TYPE_CHECKING:
    from sprites import Sprite, SpritesClient

_TRUNCATION_MARKER = b"\n...[AI-Q Sprites output truncated]...\n"
_COMMAND_ENV = "AIQ_SPRITES_COMMAND"
_COMMAND_TIMEOUT_ENV = "AIQ_SPRITES_TIMEOUT_SECONDS"
_TRANSPORT_TIMEOUT_GRACE_SECONDS = 5.0
_CLOSE_ATTEMPTS = 3
_CLOSE_RETRY_DELAY_SECONDS = 1.0


class SpriteOwnershipError(RuntimeError):
    """Raised rather than deleting a Sprite whose ownership marker changed."""


def _bounded_bytes(data: bytes, limit: int) -> tuple[bytes, bool]:
    """Keep a bounded head and tail while making truncation explicit."""
    if len(data) <= limit:
        return data, False
    content_budget = max(0, limit - len(_TRUNCATION_MARKER))
    head_bytes = content_budget // 2
    tail_bytes = content_budget - head_bytes
    tail = data[-tail_bytes:] if tail_bytes else b""
    return data[:head_bytes] + _TRUNCATION_MARKER + tail, True


def _execute_wrapper(limit: int) -> str:
    """Build a bounded process-group runner with in-Sprite timeout enforcement."""
    content_budget = max(0, limit - len(_TRUNCATION_MARKER))
    script = f"""
import os
import signal
import subprocess
import sys
import threading

budget = {content_budget}
head_budget = budget // 2
tail_budget = budget - head_budget
marker = {_TRUNCATION_MARKER!r}
command = os.environ[{_COMMAND_ENV!r}]
raw_timeout = os.environ.get({_COMMAND_TIMEOUT_ENV!r})
command_timeout = float(raw_timeout) if raw_timeout else None
head = bytearray()
tail = bytearray()
total = 0

process = subprocess.Popen(
    ["bash", "-lc", command],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
assert process.stdout is not None

def drain_output():
    global total
    while True:
        try:
            chunk = process.stdout.read(65536)
        except (OSError, ValueError):
            return
        if not chunk:
            return
        total += len(chunk)
        if len(head) < head_budget:
            take = min(head_budget - len(head), len(chunk))
            head.extend(chunk[:take])
            chunk = chunk[take:]
        if chunk and tail_budget:
            tail.extend(chunk)
            if len(tail) > tail_budget:
                del tail[:-tail_budget]

reader = threading.Thread(target=drain_output, daemon=True)
reader.start()
timed_out = False
try:
    return_code = process.wait(timeout=command_timeout)
except subprocess.TimeoutExpired:
    timed_out = True
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return_code = process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return_code = process.wait()

reader.join(timeout=1.0)
if reader.is_alive():
    process.stdout.close()
    reader.join(timeout=1.0)

if total > budget:
    output = bytes(head) + marker + bytes(tail)
else:
    output = bytes(head) + bytes(tail)
sys.stdout.buffer.write(output)
if timed_out:
    sys.stdout.buffer.write(("\\nCommand timed out after " + raw_timeout + "s.\\n").encode())
    return_code = 124
elif return_code < 0:
    return_code = 128 - return_code
sys.stdout.buffer.flush()
raise SystemExit(return_code)
""".strip()
    return f"python3 -c {shlex.quote(script)}"


def _exec_error_code(exc: ExecError) -> int:
    value = exc.exit_code
    return int(value() if callable(value) else value)


def _file_error(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError_):
        return "file_not_found"
    if isinstance(exc, IsADirectoryError_):
        return "is_directory"
    if isinstance(exc, PermissionError_):
        return "permission_denied"
    if isinstance(exc, (ValueError, UnicodeError)):
        return "invalid_path"
    if isinstance(exc, FilesystemError):
        code = (exc.code or "").upper()
        if code == "ENOENT":
            return "file_not_found"
        if code == "EISDIR":
            return "is_directory"
        if code in {"EACCES", "EPERM"}:
            return "permission_denied"
        if code in {"EINVAL", "ENAMETOOLONG"}:
            return "invalid_path"
    return f"sprites_error:{type(exc).__name__}"


class SpriteSandbox(BaseSandbox):
    """A byte-accurate Deep Agents session that owns one job-marked Sprite."""

    enable_capture_offload = True

    def __init__(
        self,
        *,
        sprite: Sprite,
        client: SpritesClient,
        ownership_label: str,
        max_output_bytes: int,
        max_download_bytes: int,
        api_timeout_seconds: float,
    ) -> None:
        self._sprite = sprite
        self._client = client
        self._ownership_label = ownership_label
        self._max_output_bytes = max_output_bytes
        self._max_download_bytes = max_download_bytes
        self._api_timeout_seconds = api_timeout_seconds
        self._closed = False

    @property
    def id(self) -> str:
        """Return the deterministic job-scoped Sprite name."""
        return self._sprite.name

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Execute a shell command with bounded, combined stdout and stderr."""
        output = io.BytesIO()
        environment = {_COMMAND_ENV: command}
        transport_timeout: float | None = None
        if timeout is not None:
            environment[_COMMAND_TIMEOUT_ENV] = str(timeout)
            transport_timeout = timeout + _TRANSPORT_TIMEOUT_GRACE_SECONDS
        cmd = self._sprite.command(
            "bash",
            "-lc",
            _execute_wrapper(self._max_output_bytes),
            env=environment,
            timeout=transport_timeout,
            stdout=output,
            stderr=output,
        )
        try:
            cmd.run()
            exit_code = 0
        except ExecError as exc:
            exit_code = _exec_error_code(exc)
        except (SpriteTimeoutError, FuturesTimeoutError):
            raw, truncated = _bounded_bytes(output.getvalue(), self._max_output_bytes)
            message = f"\nCommand timed out after {timeout}s.\n".encode()
            raw, timeout_truncated = _bounded_bytes(raw + message, self._max_output_bytes)
            return ExecuteResponse(
                output=raw.decode("utf-8", errors="replace"),
                exit_code=124,
                truncated=truncated or timeout_truncated,
            )

        raw, host_truncated = _bounded_bytes(output.getvalue(), self._max_output_bytes)
        return ExecuteResponse(
            output=raw.decode("utf-8", errors="replace"),
            exit_code=exit_code,
            truncated=host_truncated or _TRUNCATION_MARKER in raw,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload files independently so one bad path does not fail the batch."""
        filesystem = self._sprite.filesystem("/")
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                (filesystem / path).write_bytes(content, mkdir_parents=True)
                responses.append(FileUploadResponse(path=path))
            except Exception as exc:  # noqa: BLE001 - partial-success protocol requires per-file normalization
                responses.append(FileUploadResponse(path=path, error=_file_error(exc)))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Stream downloads with a hard host-memory bound, even if a file grows concurrently."""
        return [self._download_file(path) for path in paths]

    def _download_file(self, path: str) -> FileDownloadResponse:
        endpoint = f"{self._client.base_url}/v1/sprites/{quote(self._sprite.name, safe='')}/fs/read"
        headers = {
            "Authorization": f"Bearer {self._client.token}",
            "Range": f"bytes=0-{self._max_download_bytes}",
        }
        try:
            with httpx.stream(
                "GET",
                endpoint,
                headers=headers,
                params={"path": path, "workingDir": "/"},
                timeout=self._api_timeout_seconds,
            ) as response:
                if response.status_code == 404:
                    return FileDownloadResponse(path=path, error="file_not_found")
                if response.status_code not in {200, 206}:
                    detail = b""
                    for chunk in response.iter_bytes():
                        detail += chunk
                        if len(detail) >= 4096:
                            break
                    lowered = detail[:4096].decode("utf-8", errors="replace").lower()
                    if "directory" in lowered:
                        error = "is_directory"
                    elif response.status_code in {401, 403} or "permission" in lowered:
                        error = "permission_denied"
                    else:
                        error = "invalid_path"
                    return FileDownloadResponse(path=path, error=error)

                content = bytearray()
                for chunk in response.iter_bytes():
                    remaining = self._max_download_bytes + 1 - len(content)
                    if remaining <= 0:
                        break
                    content.extend(chunk[:remaining])
                    if len(content) > self._max_download_bytes:
                        return FileDownloadResponse(path=path, error="file_too_large")
                return FileDownloadResponse(path=path, content=bytes(content))
        except (httpx.HTTPError, ValueError, UnicodeError) as exc:
            return FileDownloadResponse(path=path, error=_file_error(exc))

    def close(self) -> None:
        """Destroy only the still-marked Sprite, then release the API client.

        A failed delete is retried inside this call: AI-Q drops its session
        reference before calling close() and swallows teardown errors, so a
        retry has to happen here or not at all. Whatever the outcome, the
        session latches closed and the client is released rather than left
        open. A changed ownership label is permanent, so it raises immediately
        without consuming a retry.
        """
        if self._closed:
            return
        try:
            for attempt in range(_CLOSE_ATTEMPTS):
                try:
                    self._destroy_marked_sprite()
                except SpriteOwnershipError:
                    raise
                except Exception:
                    if attempt + 1 >= _CLOSE_ATTEMPTS:
                        raise
                    time.sleep(_CLOSE_RETRY_DELAY_SECONDS)
                else:
                    return
        finally:
            self._closed = True
            self._client.close()

    def _destroy_marked_sprite(self) -> None:
        try:
            current = self._client.get_sprite(self._sprite.name)
        except NotFoundError:
            return
        if self._ownership_label not in current.labels:
            raise SpriteOwnershipError(f"Refusing to destroy Sprite {self._sprite.name!r}: ownership label changed")
        try:
            self._client.delete_sprite(self._sprite.name)
        except NotFoundError:
            pass
