"""Private status snapshots and durable admission for the two daily refreshes."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .cabinet import CabinetStatus, GippoClient, GippoError, GippoSmsRequired
from .session import ProtectedSessionStore, SessionStorageError

MINSK = ZoneInfo("Europe/Minsk")
REFRESH_HOURS = (8, 22)
ADMISSION_SECONDS = 60
MAX_CYCLE_SECONDS = 360
DEFAULT_CACHE_PATH = Path("/srv/bots/gippo-bot/private/status-cache.json")


class CacheError(RuntimeError):
    """Raised when saved status or refresh claims cannot be accessed safely."""


class CacheCycleUnavailable(CacheError):
    """Manual authentication cannot run without an active scheduled SMS cycle."""


def utc_now() -> datetime:
    """Return an aware current instant for scheduling and cycle deadlines."""

    return datetime.now(UTC)


def cache_scope(login: str, base_url: str, auth_mode: str) -> str:
    """Bind a cache to the same account, cabinet origin, and mode as its session."""

    return hashlib.sha256(f"{base_url.rstrip('/')}|{auth_mode}|{login}".encode()).hexdigest()


def scheduled_slot(now: datetime) -> str | None:
    """Identify today's slot only during its first 60 seconds in Minsk."""

    local = now.astimezone(MINSK)
    if local.hour in REFRESH_HOURS and local.minute * 60 + local.second < ADMISSION_SECONDS:
        return f"{local.date().isoformat()}/{local.hour:02d}"
    return None


def next_update(now: datetime) -> datetime:
    """Return the next future scheduled instant, without catching up missed slots."""

    local = now.astimezone(MINSK)
    for offset in (0, 1):
        day = local.date() + timedelta(days=offset)
        for hour in REFRESH_HOURS:
            candidate = datetime(day.year, day.month, day.day, hour, tzinfo=MINSK)
            if candidate > local:
                return candidate
    raise AssertionError("The next day's schedule must contain a future slot")


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Outcome of one cycle operation, with a fixed SMS continuation deadline."""

    outcome: str
    deadline: float | None = None


class StatusCache:
    """Read atomic snapshots and reserve at most two refresh cycles per local day.

    Readers never contact the cabinet and do not acquire the operation lock.
    Writers claim a slot before network I/O and hold that lock during each
    provider operation, so manual confirmation and timer polling cannot race.
    """

    def __init__(self, path: Path, scope: str) -> None:
        self._store = ProtectedSessionStore(path)
        self._scope = scope

    def read_status(self) -> CabinetStatus | None:
        """Return the last successful snapshot, even during a scheduled update."""

        try:
            ProtectedSessionStore._check_private(self._store.path.parent, directory=True)
            return self._decode_status(self._read()["snapshot"])
        except (SessionStorageError, OSError) as exc:
            raise CacheError("Could not read the protected GIPPO status cache") from exc

    def has_active_sms(self, *, now: datetime | None = None) -> bool:
        """Inspect a live scheduled challenge without network access or state changes."""

        try:
            state = self._read()
            slot = state["active"]
            cycle = state["slots"].get(slot) if slot else None
            return bool(
                cycle
                and cycle["phase"] == "waiting"
                and (now or utc_now()).timestamp() < cycle["deadline"]
            )
        except (SessionStorageError, OSError) as exc:
            raise CacheError("Could not read the protected GIPPO status cache") from exc

    def run_scheduled(
        self,
        client_factory: Callable[[], GippoClient],
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> RefreshResult:
        """Run one admitted fetch and only its existing SMS continuation.

        Duplicate, restarted, and out-of-window invocations never create a
        client or call the provider. A failed attempt consumes its slot while
        retaining the previous snapshot. SMS polling cannot initiate a login.
        """

        clock = now or utc_now
        slot = self._claim(clock())
        if slot is None:
            return RefreshResult("skipped")
        try:
            with client_factory() as client:
                result = self._perform(slot, client.fetch_status, clock)
                for _ in range(60):
                    if result.outcome != "waiting":
                        return result
                    assert result.deadline is not None
                    remaining = result.deadline - clock().timestamp()
                    if remaining <= 0:
                        break
                    sleep(min(5, remaining))
                    try:
                        result = self._perform(slot, client.poll_sms_login, clock)
                    except CacheError:
                        # Manual confirmation may hold the cache/session lock.
                        # Reading the atomic state is safe while waiting for it;
                        # never retry a provider operation without the lock.
                        state = self._read()
                        if state["active"] != slot:
                            return RefreshResult("skipped")
                        if state["slots"][slot]["phase"] != "waiting":
                            raise
                self._fail(slot)
                return RefreshResult("failed")
        except Exception:
            self._fail(slot)
            raise

    def submit_sms_code(
        self,
        client_factory: Callable[[], GippoClient],
        code: str,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> CabinetStatus:
        """Complete only an active scheduled SMS cycle and save its snapshot once.

        Codes outside that cycle never reach the provider. Invalid codes leave
        the same cycle waiting, subject to the existing session attempt limits.
        """

        clock = now or utc_now
        with self._operation():
            state = self._read()
            slot = state["active"]
            cycle = state["slots"].get(slot) if slot else None
            if (
                cycle is None
                or cycle["phase"] != "waiting"
                or clock().timestamp() >= cycle["deadline"]
            ):
                raise CacheCycleUnavailable("There is no active scheduled GIPPO SMS update")
            with client_factory() as client:
                status = client.submit_sms_code(code)
            if clock().timestamp() >= cycle["deadline"]:
                self._finish(state, slot, None)
                raise CacheCycleUnavailable("The scheduled GIPPO SMS update has expired")
            self._finish(state, slot, status)
            return status

    def _claim(self, now: datetime) -> str | None:
        slot = scheduled_slot(now)
        if slot is None:
            return None
        with self._operation():
            state = self._read()
            if slot in state["slots"]:
                return None
            previous = state["active"]
            if previous:
                state["slots"][previous]["phase"] = "failed"
            state["slots"][slot] = {
                "phase": "fetching",
                "deadline": now.timestamp() + MAX_CYCLE_SECONDS,
            }
            state["active"] = slot
            self._store.write(state)
            return slot

    def _perform(
        self, slot: str, action: Callable[[], CabinetStatus], clock: Callable[[], datetime]
    ) -> RefreshResult:
        with self._operation():
            state = self._read()
            cycle = state["slots"][slot]
            if state["active"] != slot or cycle["phase"] not in {"fetching", "waiting"}:
                return RefreshResult("skipped")
            if clock().timestamp() >= cycle["deadline"]:
                self._finish(state, slot, None)
                return RefreshResult("failed")
            try:
                status = action()
            except GippoSmsRequired as exc:
                cycle["phase"] = "waiting"
                cycle["deadline"] = min(
                    cycle["deadline"], exc.expires_at, clock().timestamp() + 300
                )
                self._store.write(state)
                return RefreshResult("waiting", cycle["deadline"])
            except GippoError:
                self._finish(state, slot, None)
                return RefreshResult("failed")
            if clock().timestamp() >= cycle["deadline"]:
                self._finish(state, slot, None)
                return RefreshResult("failed")
            self._finish(state, slot, status)
            return RefreshResult("updated")

    def _finish(self, state: dict[str, Any], slot: str, status: CabinetStatus | None) -> None:
        state["slots"][slot]["phase"] = "completed" if status else "failed"
        state["active"] = None
        if status:
            state["snapshot"] = {
                "discount_percent": str(status.discount_percent),
                "monthly_purchases": str(status.monthly_purchases),
                "until_next_level": str(status.until_next_level),
                "fetched_at": status.fetched_at.isoformat(),
            }
            self._decode_status(state["snapshot"])
        self._store.write(state)

    def _fail(self, slot: str) -> None:
        with self._operation():
            state = self._read()
            if state["active"] == slot:
                self._finish(state, slot, None)

    @contextmanager
    def _operation(self):
        try:
            with self._store.operation():
                yield
        except (SessionStorageError, OSError) as exc:
            raise CacheError("Could not access the protected GIPPO status cache") from exc

    def _read(self) -> dict[str, Any]:
        state = self._store.read()
        if state is None:
            return {
                "version": 1,
                "scope": self._scope,
                "snapshot": None,
                "slots": {},
                "active": None,
            }
        try:
            if (
                state["version"] != 1
                or state["scope"] != self._scope
                or not isinstance(state["slots"], dict)
                or (state["active"] is not None and state["active"] not in state["slots"])
            ):
                raise ValueError
            for slot, cycle in state["slots"].items():
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}/(?:08|22)", slot):
                    raise ValueError
                datetime.fromisoformat(slot.split("/")[0])
                if (
                    cycle["phase"] not in {"fetching", "waiting", "completed", "failed"}
                    or not isinstance(cycle["deadline"], (int, float))
                    or not 0 < cycle["deadline"] < 253402300799
                ):
                    raise ValueError
            self._decode_status(state["snapshot"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise CacheError(
                "The protected GIPPO status cache is invalid or belongs to another account"
            ) from exc
        return state

    @staticmethod
    def _decode_status(value: Any) -> CabinetStatus | None:
        if value is None:
            return None
        try:
            fields = [
                Decimal(value[key])
                for key in ("discount_percent", "monthly_purchases", "until_next_level")
            ]
            fetched_at = datetime.fromisoformat(value["fetched_at"])
            if (
                not all(number.is_finite() and number >= 0 for number in fields)
                or fetched_at.tzinfo is None
            ):
                raise ValueError
            return CabinetStatus(*fields, fetched_at.astimezone(UTC))
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError) as exc:
            raise CacheError("The protected GIPPO status snapshot is invalid") from exc
