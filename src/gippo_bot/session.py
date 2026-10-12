"""Protected, atomic session storage with a process-wide operation lock."""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class SessionStorageError(RuntimeError):
    """Raised when private session state cannot be accessed safely."""


def read_protected_token(path: Path) -> str:
    """Read one token from an owner-only file without logging its contents."""

    ProtectedSessionStore._check_private(path.parent, directory=True)
    ProtectedSessionStore._check_private(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            value = source.read(4097).strip()
    except (OSError, UnicodeError) as exc:
        raise SessionStorageError("The SMS forwarding token file cannot be read") from exc
    if not value or len(value) > 4096 or any(character.isspace() for character in value):
        raise SessionStorageError("The SMS forwarding token file must contain exactly one token")
    return value


class ProtectedSessionStore:
    """Store secret cookies in an owner-only file without partial writes."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def operation(self):
        """Prevent two processes from sending SMS or changing cookies together."""

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_private(self.path.parent, directory=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        self._check_private(lock_path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        with os.fdopen(descriptor, "r+b") as lock:
            locked = False
            try:
                if os.name == "nt":
                    import msvcrt

                    if os.fstat(lock.fileno()).st_size == 0:
                        lock.write(b"\0")
                        lock.flush()
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                yield
            except OSError as exc:
                raise SessionStorageError(
                    "The private GIPPO session is busy or unavailable"
                ) from exc
            finally:
                if locked:
                    if os.name == "nt":
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, Any] | None:
        """Read validated JSON state; fail closed if an existing file is damaged."""

        self._check_private(self.path)
        try:
            with self.path.open(encoding="utf-8") as source:
                value = json.load(source)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise SessionStorageError("The private GIPPO session file cannot be read") from exc
        if not isinstance(value, dict):
            raise SessionStorageError("The private GIPPO session file has an invalid format")
        return value

    def write(self, value: dict[str, Any]) -> None:
        """Replace session JSON atomically with mode 0600 on POSIX systems."""

        self._check_private(self.path)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.new")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump(value, target, ensure_ascii=True, separators=(",", ":"))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise SessionStorageError("The private GIPPO session file cannot be saved") from exc
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _check_private(path: Path, *, directory: bool = False) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or (
            not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode)
        ):
            raise SessionStorageError("The private GIPPO session path is unsafe")
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise SessionStorageError("The private GIPPO session must be owner-only")
