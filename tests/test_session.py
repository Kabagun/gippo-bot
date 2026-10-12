import os
from pathlib import Path

import pytest

from gippo_bot.session import ProtectedSessionStore, SessionStorageError, read_protected_token


def test_session_operation_lock_prevents_another_process_owner(tmp_path: Path) -> None:
    first = ProtectedSessionStore(tmp_path / "session.json")
    second = ProtectedSessionStore(tmp_path / "session.json")
    with (
        first.operation(),
        pytest.raises(SessionStorageError, match="busy"),
        second.operation(),
    ):
        pytest.fail("The second owner acquired an active operation lock")
    with second.operation():
        second.write({"state": "new"})
        assert second.read() == {"state": "new"}


@pytest.mark.parametrize("content", ["", "token another", "token\nother", "a" * 4097])
def test_token_file_requires_one_bounded_token(tmp_path: Path, content: str) -> None:
    path = tmp_path / "token"
    path.write_text(content)
    path.chmod(0o600)
    with pytest.raises(SessionStorageError, match="one token"):
        read_protected_token(path)


@pytest.mark.skipif(os.name == "nt", reason="Windows private ACLs are configured by the operator")
def test_posix_session_and_token_reject_public_permissions(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("secret")
    path.chmod(0o644)
    with pytest.raises(SessionStorageError, match="owner-only"):
        read_protected_token(path)
    with pytest.raises(SessionStorageError, match="owner-only"):
        ProtectedSessionStore(path).read()


@pytest.mark.skipif(
    os.name == "nt", reason="Creating a Windows symlink requires optional privileges"
)
def test_session_and_token_reject_symlinks(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("token")
    secret.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(secret)
    with pytest.raises(SessionStorageError, match="unsafe"):
        read_protected_token(link)
    with pytest.raises(SessionStorageError, match="unsafe"):
        ProtectedSessionStore(link).read()
