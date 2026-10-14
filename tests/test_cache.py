import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

from gippo_bot.cabinet import CabinetStatus, GippoAuthenticationError, GippoError, GippoSmsRequired
from gippo_bot.cache import (
    ADMISSION_SECONDS,
    MINSK,
    REFRESH_HOURS,
    CacheCycleUnavailable,
    CacheError,
    StatusCache,
    cache_scope,
    next_update,
    scheduled_slot,
)


def instant(day=14, hour=8, minute=0, second=0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=MINSK)


def status(at=None, purchases="247.27") -> CabinetStatus:
    return CabinetStatus(Decimal("1"), Decimal(purchases), Decimal("252.73"), at or instant())


def provider(result=None) -> Mock:
    value = Mock()
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=None)
    if isinstance(result, Exception):
        value.fetch_status.side_effect = result
    else:
        value.fetch_status.return_value = result or status()
    return value


def cache(tmp_path: Path) -> StatusCache:
    return StatusCache(
        tmp_path / "cache.json", cache_scope("login", "https://cabinet.gippo.by", "sms")
    )


@pytest.mark.parametrize(
    ("hour", "minute", "second", "accepted"),
    [
        (8, 0, 0, True),
        (8, 0, 59, True),
        (8, 1, 0, False),
        (22, 0, 0, True),
        (22, 0, 59, True),
        (22, 1, 0, False),
        (7, 59, 59, False),
        (12, 0, 0, False),
        (20, 0, 0, False),
    ],
)
def test_minsk_admission_window(hour, minute, second, accepted) -> None:
    assert (
        scheduled_slot(instant(hour=hour, minute=minute, second=second)) is not None
    ) is accepted


def test_slots_use_minsk_date_and_next_future_update() -> None:
    assert scheduled_slot(datetime(2026, 10, 14, 5, 0, tzinfo=UTC)) == "2026-10-14/08"
    assert scheduled_slot(datetime(2026, 10, 14, 19, 0, tzinfo=UTC)) == "2026-10-14/22"
    assert next_update(datetime(2026, 10, 14, 21, 1, tzinfo=UTC)) == instant(day=15)
    assert next_update(instant(hour=22)) == instant(day=15)
    assert next_update(instant(hour=8, minute=1)) == instant(hour=22)


def test_duplicate_restart_and_failed_slots_consume_exactly_two_daily_attempts(tmp_path) -> None:
    factory = Mock(return_value=provider(GippoError("offline")))
    first = cache(tmp_path)
    for hour in REFRESH_HOURS:
        assert (
            first.run_scheduled(factory, now=lambda hour=hour: instant(hour=hour)).outcome
            == "failed"
        )
        restarted = cache(tmp_path)
        assert (
            restarted.run_scheduled(
                factory, now=lambda hour=hour: instant(hour=hour, second=59)
            ).outcome
            == "skipped"
        )
    assert factory.call_count == 2
    assert first.read_status() is None
    assert first.run_scheduled(factory, now=lambda: instant(day=15)).outcome == "failed"
    assert factory.call_count == 3
    persisted = json.loads((tmp_path / "cache.json").read_text())
    assert set(persisted["slots"]) == {"2026-10-14/08", "2026-10-14/22", "2026-10-15/08"}


def test_out_of_window_never_creates_provider_or_bootstraps_cache(tmp_path) -> None:
    factory = Mock(side_effect=AssertionError("must not create provider"))
    saved = cache(tmp_path)
    for hour in (0, 7, 9, 16, 20, 23):
        assert (
            saved.run_scheduled(factory, now=lambda hour=hour: instant(hour=hour)).outcome
            == "skipped"
        )
    assert saved.run_scheduled(factory, now=lambda: instant(minute=1)).outcome == "skipped"
    assert not (tmp_path / "cache.json").exists()
    assert saved.read_status() is None
    factory.assert_not_called()


def test_snapshot_survives_restart_and_failed_next_slot(tmp_path) -> None:
    morning = status()
    saved = cache(tmp_path)
    assert saved.run_scheduled(lambda: provider(morning), now=instant).outcome == "updated"
    restarted = cache(tmp_path)
    assert restarted.read_status() == morning
    assert (
        restarted.run_scheduled(
            lambda: provider(GippoError("failed")), now=lambda: instant(hour=22)
        ).outcome
        == "failed"
    )
    assert cache(tmp_path).read_status() == morning


def test_in_progress_cycle_serves_old_snapshot_without_locking_readers(tmp_path) -> None:
    saved = cache(tmp_path)
    old = status()
    saved.run_scheduled(lambda: provider(old), now=instant)
    entered, finish = Event(), Event()
    fresh = provider()

    def block_fetch():
        entered.set()
        assert finish.wait(3)
        return status(instant(hour=22), "300.00")

    fresh.fetch_status.side_effect = block_fetch
    factory = Mock(return_value=fresh)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(saved.run_scheduled, factory, now=lambda: instant(hour=22))
        assert entered.wait(3)
        assert cache(tmp_path).read_status() == old
        with pytest.raises(CacheError):
            cache(tmp_path).run_scheduled(factory, now=lambda: instant(hour=22))
        finish.set()
        assert pending.result(3).outcome == "updated"
    assert factory.call_count == fresh.fetch_status.call_count == 1


@pytest.mark.parametrize("contents", ["broken", "[]", '{"version":1}', '{"snapshot":null}'])
def test_invalid_cache_fails_closed_without_provider_access(tmp_path, contents) -> None:
    path = tmp_path / "cache.json"
    path.write_text(contents)
    path.chmod(0o600)
    factory = Mock()
    saved = cache(tmp_path)
    with pytest.raises(CacheError):
        saved.read_status()
    with pytest.raises(CacheError):
        saved.run_scheduled(factory, now=instant)
    factory.assert_not_called()


def test_scope_mismatch_never_reuses_or_overwrites_another_account(tmp_path) -> None:
    saved = cache(tmp_path)
    saved.run_scheduled(lambda: provider(), now=instant)
    foreign = StatusCache(tmp_path / "cache.json", "different-account")
    with pytest.raises(CacheError):
        foreign.read_status()
    with pytest.raises(CacheError):
        foreign.run_scheduled(Mock(), now=lambda: instant(hour=22))
    assert saved.read_status() == status()


def test_same_job_sms_continuation_never_calls_fetch_status_twice(tmp_path) -> None:
    clock = [instant()]
    fresh = provider(GippoSmsRequired(automatic=True, expires_at=clock[0].timestamp() + 15))
    fresh.poll_sms_login.side_effect = [
        GippoSmsRequired(automatic=True, expires_at=clock[0].timestamp() + 15),
        status(),
    ]
    delays = []

    def sleep(seconds):
        delays.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    saved = cache(tmp_path)
    assert (
        saved.run_scheduled(lambda: fresh, now=lambda: clock[0], sleep=sleep).outcome == "updated"
    )
    assert delays == [5, 5]
    assert fresh.fetch_status.call_count == 1
    assert fresh.poll_sms_login.call_count == 2
    assert saved.read_status() == status()


def test_sms_deadline_does_not_extend_or_retry_generic_failure(tmp_path) -> None:
    clock = [instant()]
    pending = GippoSmsRequired(automatic=True, expires_at=clock[0].timestamp() + 12)
    fresh = provider(pending)
    fresh.poll_sms_login.side_effect = pending
    saved = cache(tmp_path)

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    assert saved.run_scheduled(lambda: fresh, now=lambda: clock[0], sleep=sleep).outcome == "failed"
    assert fresh.fetch_status.call_count == 1 and fresh.poll_sms_login.call_count == 2
    assert clock[0] == instant(second=12)
    assert saved.run_scheduled(Mock(), now=lambda: instant(second=30)).outcome == "skipped"
    fresh = provider(GippoError("network"))
    assert saved.run_scheduled(lambda: fresh, now=lambda: instant(hour=22)).outcome == "failed"
    fresh.poll_sms_login.assert_not_called()


def test_manual_confirmation_commits_once_and_timer_stops_before_next_poll(tmp_path) -> None:
    saved = cache(tmp_path)
    clock = [instant()]
    waiting = provider(GippoSmsRequired(automatic=False, expires_at=clock[0].timestamp() + 300))
    manual = provider()
    manual.submit_sms_code.return_value = status()
    manual_factory = Mock(return_value=manual)

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
        assert saved.has_active_sms(now=clock[0])
        saved.submit_sms_code(manual_factory, "12345", now=lambda: clock[0])

    assert (
        saved.run_scheduled(lambda: waiting, now=lambda: clock[0], sleep=sleep).outcome == "skipped"
    )
    assert saved.read_status() == status()
    waiting.poll_sms_login.assert_not_called()
    manual.fetch_status.assert_not_called()
    manual.submit_sms_code.assert_called_once_with("12345")
    with pytest.raises(CacheCycleUnavailable):
        saved.submit_sms_code(manual_factory, "12345", now=lambda: clock[0])
    assert manual_factory.call_count == 1


def test_manual_confirmation_outside_cycle_never_creates_provider(tmp_path) -> None:
    saved = cache(tmp_path)
    factory = Mock()
    for hour in (8, 12, 22):
        with pytest.raises(CacheCycleUnavailable):
            saved.submit_sms_code(factory, "12345", now=lambda hour=hour: instant(hour=hour))
    saved.run_scheduled(lambda: provider(), now=instant)
    with pytest.raises(CacheCycleUnavailable):
        saved.submit_sms_code(factory, "12345", now=lambda: instant(hour=22))
    factory.assert_not_called()


def test_timer_contract_matches_guard_and_does_not_catch_up() -> None:
    timer = (Path(__file__).parents[1] / "deploy" / "gippo-cache-refresh.timer").read_text()
    assert REFRESH_HOURS == (8, 22) and ADMISSION_SECONDS == 60
    for hour in REFRESH_HOURS:
        assert f"OnCalendar=*-*-* {hour:02d}:00:00 {MINSK.key}" in timer
    assert "Persistent=false" in timer
    assert "AccuracySec=1s" in timer and "RandomizedDelaySec=0" in timer
    assert "OnBootSec" not in timer and "OnStartupSec" not in timer


def test_full_scheduled_sms_job_uses_one_cabinet_fetch_and_five_digit_code(
    tmp_path, monkeypatch
) -> None:
    from test_sms_login import CabinetServer, ForwarderServer, client

    clock = [instant()]
    epoch = [clock[0].timestamp()]
    monkeypatch.setattr("gippo_bot.cabinet.time.time", lambda: epoch[0])
    server, forwarder = CabinetServer(), ForwarderServer(epoch)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += timedelta(seconds=seconds)
        epoch[0] = clock[0].timestamp()
        if len(sleeps) == 3:
            forwarder.status = "received"

    saved = cache(tmp_path)
    result = saved.run_scheduled(
        lambda: client(server, tmp_path / "session.json", forwarder),
        now=lambda: clock[0],
        sleep=sleep,
    )
    assert result.outcome == "updated" and sleeps == [5, 5, 5]
    assert saved.read_status().monthly_purchases == Decimal("247.27")
    assert server.sms_requests == server.code_requests == server.personal_requests == 1
    assert forwarder.calls.count(("POST", "/internal/v1/challenges")) == 1


def test_manual_code_after_original_deadline_cannot_extend_cycle(tmp_path) -> None:
    clock = [instant()]
    saved = cache(tmp_path)
    pending = provider(GippoSmsRequired(automatic=False, expires_at=clock[0].timestamp() + 60))
    manual_factory = Mock()

    def sleep(seconds):
        clock[0] += timedelta(seconds=61)
        with pytest.raises(CacheCycleUnavailable):
            saved.submit_sms_code(manual_factory, "12345", now=lambda: clock[0])

    assert (
        saved.run_scheduled(lambda: pending, now=lambda: clock[0], sleep=sleep).outcome == "failed"
    )
    manual_factory.assert_not_called()
    pending.poll_sms_login.assert_not_called()


def test_bad_manual_code_retains_old_snapshot_and_original_deadline(tmp_path) -> None:
    saved = cache(tmp_path)
    old = status(instant(day=13))
    saved.run_scheduled(lambda: provider(old), now=lambda: instant(day=13))
    clock = [instant()]
    required = GippoSmsRequired(automatic=False, expires_at=clock[0].timestamp() + 30)
    waiting = provider(required)
    waiting.poll_sms_login.side_effect = required
    bad_code = provider()
    bad_code.submit_sms_code.side_effect = GippoAuthenticationError("incorrect code")
    attempted = []

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
        if not attempted:
            attempted.append(True)
            before = json.loads((tmp_path / "cache.json").read_text())
            with pytest.raises(GippoAuthenticationError):
                saved.submit_sms_code(lambda: bad_code, "12345", now=lambda: clock[0])
            after = json.loads((tmp_path / "cache.json").read_text())
            assert (
                after["slots"][after["active"]]["deadline"]
                == before["slots"][before["active"]]["deadline"]
            )
            assert saved.read_status() == old

    assert (
        saved.run_scheduled(lambda: waiting, now=lambda: clock[0], sleep=sleep).outcome == "failed"
    )
    assert saved.read_status() == old
    assert clock[0] == instant(second=30)
    bad_code.submit_sms_code.assert_called_once_with("12345")
    bad_code.fetch_status.assert_not_called()
