from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections.abc import Iterator

import pytest
from sprites import FileNotFoundError_, NotFoundError

from aiq_sprites.sandbox import _CLOSE_ATTEMPTS, SpriteOwnershipError, SpriteSandbox, _execute_wrapper
from tests.fakes import FakeClient, FakeSprite


def make_session(
    sprite: FakeSprite | None = None,
    client: FakeClient | None = None,
    *,
    max_output_bytes: int = 4096,
    max_download_bytes: int = 8,
) -> tuple[SpriteSandbox, FakeSprite, FakeClient]:
    sprite = sprite or FakeSprite("aiq-test", ["aiq-sandbox", "aiq-job-owner"])
    client = client or FakeClient(sprite=sprite)
    session = SpriteSandbox(
        sprite=sprite,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        ownership_label="aiq-job-owner",
        max_output_bytes=max_output_bytes,
        max_download_bytes=max_download_bytes,
        api_timeout_seconds=5.0,
    )
    return session, sprite, client


def test_execute_keeps_original_command_in_environment() -> None:
    session, sprite, _ = make_session()
    sprite.command_results.append((b"hello\n", 0, False))

    result = session.execute("printf 'secret-command'")

    assert result.output == "hello\n"
    assert result.exit_code == 0
    assert result.truncated is False
    invocation = sprite.commands[0]
    assert invocation["args"][:2] == ("bash", "-lc")
    assert "secret-command" not in invocation["args"][2]
    assert invocation["env"] == {"AIQ_SPRITES_COMMAND": "printf 'secret-command'"}


def test_execute_preserves_nonzero_exit_and_bounds_output() -> None:
    session, sprite, _ = make_session(max_output_bytes=4096)
    sprite.command_results.append((b"x" * 8000, 17, False))

    result = session.execute("bad-command")

    assert result.exit_code == 17
    assert result.truncated is True
    assert len(result.output.encode()) <= 4096
    assert "output truncated" in result.output


def test_execute_normalizes_timeout() -> None:
    session, sprite, _ = make_session()
    sprite.command_results.append((b"partial", 0, True))

    result = session.execute("sleep 60", timeout=3)

    assert result.exit_code == 124
    assert "timed out after 3s" in result.output


def test_execute_normalizes_client_future_timeout() -> None:
    session, sprite, _ = make_session()

    class ClientTimedOutCommand:
        @staticmethod
        def run() -> None:
            raise TimeoutError

    sprite.command = lambda *args, **kwargs: ClientTimedOutCommand()  # type: ignore[method-assign]

    result = session.execute("sleep 60", timeout=3)

    assert result.exit_code == 124
    assert "timed out after 3s" in result.output


def test_execute_wrapper_really_drains_bounds_and_preserves_exit_code() -> None:
    environment = dict(os.environ)
    environment["AIQ_SPRITES_COMMAND"] = "python3 -c \"print('x' * 10000)\"; exit 23"

    result = subprocess.run(
        ["bash", "-lc", _execute_wrapper(4096)],
        env=environment,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 23
    assert len(result.stdout) <= 4096
    assert b"output truncated" in result.stdout
    assert result.stderr == b""


def test_execute_wrapper_kills_the_remote_process_group_on_timeout(tmp_path: object) -> None:
    marker = str(tmp_path) + "/command-finished"
    environment = dict(os.environ)
    environment["AIQ_SPRITES_COMMAND"] = f"sleep 1; touch {shlex.quote(marker)}"
    environment["AIQ_SPRITES_TIMEOUT_SECONDS"] = "0.1"

    result = subprocess.run(
        ["bash", "-lc", _execute_wrapper(4096)],
        env=environment,
        capture_output=True,
        check=False,
        timeout=5,
    )
    time.sleep(1.0)

    assert result.returncode == 124
    assert b"timed out after 0.1s" in result.stdout
    assert not os.path.exists(marker)


def test_upload_files_reports_partial_success() -> None:
    session, sprite, _ = make_session()
    sprite.files.errors["/missing/file"] = FileNotFoundError_("write", "/missing/file")

    responses = session.upload_files([("/ok", b"ok"), ("/missing/file", b"bad")])

    assert responses[0].error is None
    assert sprite.files.files["/ok"] == b"ok"
    assert responses[1].error == "file_not_found"


class StreamResponse:
    def __init__(self, status_code: int, chunks: list[bytes]) -> None:
        self.status_code = status_code
        self.chunks = chunks

    def __enter__(self) -> StreamResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def iter_bytes(self) -> Iterator[bytes]:
        yield from self.chunks


@pytest.mark.parametrize(
    ("response", "expected_content", "expected_error"),
    [
        (StreamResponse(206, [b"123", b"456"]), b"123456", None),
        (StreamResponse(206, [b"123456789"]), None, "file_too_large"),
        (StreamResponse(404, []), None, "file_not_found"),
        (StreamResponse(400, [b"is a directory"]), None, "is_directory"),
    ],
)
def test_download_files_are_streamed_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    response: StreamResponse,
    expected_content: bytes | None,
    expected_error: str | None,
) -> None:
    session, _, _ = make_session(max_download_bytes=8)
    monkeypatch.setattr("aiq_sprites.sandbox.httpx.stream", lambda *args, **kwargs: response)

    result = session.download_files(["/artifact"])[0]

    assert result.content == expected_content
    assert result.error == expected_error


def test_close_rechecks_ownership_and_is_idempotent() -> None:
    session, _, client = make_session()

    session.close()
    session.close()

    assert client.deleted == ["aiq-test"]
    assert client.closed is True


def test_close_refuses_to_delete_replaced_sprite() -> None:
    sprite = FakeSprite("aiq-test", ["aiq-sandbox", "different-owner"])
    session, _, client = make_session(sprite=sprite, client=FakeClient(sprite=sprite))

    with pytest.raises(SpriteOwnershipError):
        session.close()

    assert client.deleted == []
    assert client.closed is True

    # A changed ownership label is permanent, so it latches: no retry, no delete.
    session.close()

    assert client.deleted == []


def test_close_retries_a_transient_delete_within_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiq_sprites.sandbox.time.sleep", lambda _: None)
    session, _, client = make_session()
    attempts: list[str] = []
    succeeding_delete = client.delete_sprite

    def flaky_delete(name: str) -> None:
        attempts.append(name)
        if len(attempts) == 1:
            raise RuntimeError("transient delete failure")
        succeeding_delete(name)

    client.delete_sprite = flaky_delete  # type: ignore[method-assign]

    session.close()

    assert attempts == ["aiq-test", "aiq-test"]
    assert client.deleted == ["aiq-test"]
    assert client.closed is True


def test_close_gives_up_after_bounded_retries_and_still_releases_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("aiq_sprites.sandbox.time.sleep", lambda _: None)
    session, _, client = make_session()
    attempts: list[str] = []

    def failing_delete(name: str) -> None:
        attempts.append(name)
        raise RuntimeError("transient delete failure")

    client.delete_sprite = failing_delete  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="transient delete failure"):
        session.close()

    assert attempts == ["aiq-test"] * _CLOSE_ATTEMPTS
    assert client.deleted == []
    assert client.closed is True

    # Giving up latches the session closed: the client is gone, so a later
    # close() has nothing to retry with and does not pay for more attempts.
    session.close()

    assert attempts == ["aiq-test"] * _CLOSE_ATTEMPTS


def test_close_retries_a_transient_ownership_recheck_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiq_sprites.sandbox.time.sleep", lambda _: None)
    session, _, client = make_session()
    real_get = client.get_sprite
    calls = 0

    def flaky_get(name: str) -> FakeSprite:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient api failure")
        return real_get(name)

    client.get_sprite = flaky_get  # type: ignore[method-assign]

    session.close()

    assert calls == 2
    assert client.deleted == ["aiq-test"]
    assert client.closed is True


def test_close_releases_the_client_when_the_ownership_recheck_keeps_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("aiq_sprites.sandbox.time.sleep", lambda _: None)
    session, _, client = make_session()
    attempts = 0

    def failing_get(name: str) -> FakeSprite:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("transient api failure")

    client.get_sprite = failing_get  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="transient api failure"):
        session.close()

    assert attempts == _CLOSE_ATTEMPTS
    assert client.deleted == []
    assert client.closed is True


def test_close_treats_an_already_deleted_sprite_as_done() -> None:
    session, _, client = make_session()
    attempts = 0

    def gone_delete(name: str) -> None:
        nonlocal attempts
        attempts += 1
        raise NotFoundError(name)

    client.delete_sprite = gone_delete  # type: ignore[method-assign]

    session.close()

    # The Sprite is already gone, which is the desired end state: no retry needed.
    assert attempts == 1
    assert client.closed is True


def test_close_refuses_to_delete_when_ownership_flips_between_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ownership recheck runs on every attempt, not just the first."""
    monkeypatch.setattr("aiq_sprites.sandbox.time.sleep", lambda _: None)
    sprite = FakeSprite("aiq-test", ["aiq-sandbox", "aiq-job-owner"])
    session, _, client = make_session(sprite=sprite, client=FakeClient(sprite=sprite))
    real_get = client.get_sprite
    calls = 0

    def flaky_get(name: str) -> FakeSprite:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient api failure")
        client.sprite = FakeSprite(name, ["aiq-sandbox", "different-owner"])
        return real_get(name)

    client.get_sprite = flaky_get  # type: ignore[method-assign]

    with pytest.raises(SpriteOwnershipError):
        session.close()

    assert client.deleted == []
    assert client.closed is True


def test_close_releases_the_client_when_the_sprite_is_already_gone() -> None:
    session, _, client = make_session()
    client.sprite = None

    session.close()
    session.close()

    assert client.deleted == []
    assert client.closed is True
