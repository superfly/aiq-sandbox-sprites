"""Small stateful Sprites SDK fakes used by provider contract tests."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from sprites import NetworkPolicy
from sprites.exceptions import ExecError
from sprites.exceptions import TimeoutError as SpriteTimeoutError


@dataclass
class FakeResponse:
    status_code: int
    text: str = ""

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


class FakePath:
    def __init__(self, filesystem: FakeFilesystem, path: str) -> None:
        self.filesystem = filesystem
        self.path = path

    def write_bytes(self, content: bytes, *, mkdir_parents: bool = True) -> None:
        if self.path in self.filesystem.errors:
            raise self.filesystem.errors[self.path]
        self.filesystem.files[self.path] = content


class FakeFilesystem:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.errors: dict[str, Exception] = {}

    def __truediv__(self, path: str) -> FakePath:
        return FakePath(self, path)


class FakeCommand:
    def __init__(
        self,
        *,
        output: Any,
        payload: bytes,
        exit_code: int,
        times_out: bool,
    ) -> None:
        self.output = output
        self.payload = payload
        self.exit_code = exit_code
        self.times_out = times_out

    def run(self) -> None:
        self.output.write(self.payload)
        if self.times_out:
            raise SpriteTimeoutError("timed out")
        if self.exit_code:
            raise ExecError("failed", self.exit_code)


class FakeSprite:
    def __init__(self, name: str, labels: list[str]) -> None:
        self.name = name
        self.labels = labels
        self.files = FakeFilesystem()
        self.commands: list[dict[str, Any]] = []
        self.command_results: list[tuple[bytes, int, bool]] = []
        self.run_results: list[SimpleNamespace] = []
        self.network_policy = NetworkPolicy()

    def command(self, *args: str, **kwargs: Any) -> FakeCommand:
        self.commands.append({"args": args, **kwargs})
        payload, exit_code, times_out = self.command_results.pop(0) if self.command_results else (b"", 0, False)
        return FakeCommand(
            output=kwargs["stdout"],
            payload=payload,
            exit_code=exit_code,
            times_out=times_out,
        )

    def filesystem(self, working_dir: str = "/") -> FakeFilesystem:
        return self.files

    def update_network_policy(self, policy: NetworkPolicy) -> None:
        self.network_policy = policy

    def get_network_policy(self) -> NetworkPolicy:
        return self.network_policy

    def run(self, *args: str, **kwargs: Any) -> SimpleNamespace:
        if not self.run_results:
            raise AssertionError("unexpected network probe")
        return self.run_results.pop(0)


class FakeClient:
    def __init__(
        self,
        *,
        token: str = "test-token",
        base_url: str = "https://api.sprites.test",
        timeout: float = 30.0,
        create_status: int = 201,
        sprite: FakeSprite | None = None,
    ) -> None:
        self.token = token
        self.base_url = base_url
        self.timeout = timeout
        self.create_status = create_status
        self.sprite = sprite
        self.http_client = self
        self.create_requests: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.create_requests.append({"url": url, **kwargs})
        return FakeResponse(self.create_status)

    def get_sprite(self, name: str) -> FakeSprite:
        if self.sprite is None:
            from sprites import NotFoundError

            raise NotFoundError(name)
        return self.sprite

    def delete_sprite(self, name: str) -> None:
        self.deleted.append(name)
        self.sprite = None

    def close(self) -> None:
        self.closed = True


def completed(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
