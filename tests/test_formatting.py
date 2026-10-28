from datetime import UTC, datetime
from decimal import Decimal

import pytest

from gippo_bot.cabinet import CabinetStatus
from gippo_bot.formatting import format_cached_status, format_empty_cache, format_status


def test_format_status_uses_russian_decimal_notation() -> None:
    status = CabinetStatus(
        discount_percent=Decimal("1"),
        monthly_purchases=Decimal("247.2"),
        until_next_level=Decimal("252.73"),
        fetched_at=datetime.now(UTC),
    )

    assert format_status(status) == (
        "🏷 Текущая скидка: 1%\n"
        "✨ Скидка в следующем месяце: 1%\n"
        "Покупки в этом месяце: 247,20 руб.\n"
        "До следующего уровня: 252,73 руб."
    )


@pytest.mark.parametrize(
    ("monthly_purchases", "expected_discount"),
    (
        (Decimal("499.99"), "1%"),
        (Decimal("500"), "3%"),
        (Decimal("999.99"), "3%"),
        (Decimal("1000"), "5%"),
    ),
)
def test_format_status_projects_next_month_discount_at_tier_boundaries(
    monthly_purchases: Decimal, expected_discount: str
) -> None:
    status = CabinetStatus(
        discount_percent=Decimal("1"),
        monthly_purchases=monthly_purchases,
        until_next_level=Decimal("0"),
        fetched_at=datetime.now(UTC),
    )

    projected_line = format_status(status).splitlines()[1]

    assert projected_line == f"✨ Скидка в следующем месяце: {expected_discount}"


def test_format_status_omits_next_level_at_highest_tier() -> None:
    status = CabinetStatus(
        discount_percent=Decimal("5"),
        monthly_purchases=Decimal("1022"),
        until_next_level=Decimal("0"),
        fetched_at=datetime.now(UTC),
    )

    assert format_status(status) == (
        "🏷 Текущая скидка: 5%\n"
        "✨ Скидка в следующем месяце: 5%\n"
        "Покупки в этом месяце: 1 022,00 руб."
    )


def test_format_status_bolds_discount_lines_for_telegram() -> None:
    status = CabinetStatus(
        discount_percent=Decimal("5"),
        monthly_purchases=Decimal("1022"),
        until_next_level=Decimal("0"),
        fetched_at=datetime.now(UTC),
    )

    assert format_status(status, telegram_html=True) == (
        "<b>🏷 Текущая скидка: 5%</b>\n"
        "<b>✨ Скидка в следующем месяце: 5%</b>\n"
        "Покупки в этом месяце: 1 022,00 руб."
    )


@pytest.mark.parametrize(
    ("received", "current", "expected"),
    [
        (
            datetime(2026, 10, 30, 19, 0, tzinfo=UTC),
            datetime(2026, 10, 30, 20, 0, tzinfo=UTC),
            "30.10.2026 в 22:00",
        ),
        (
            datetime(2026, 10, 30, 19, 0, tzinfo=UTC),
            datetime(2026, 10, 30, 21, 30, tzinfo=UTC),
            "30.10.2026 в 22:00",
        ),
        (
            datetime(2026, 10, 30, 19, 0, tzinfo=UTC),
            datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
            "30.10.2026 в 22:00",
        ),
        (
            datetime(2026, 10, 30, 21, 5, tzinfo=UTC),
            datetime(2026, 10, 30, 21, 30, tzinfo=UTC),
            "31.10.2026 в 00:05",
        ),
    ],
)
def test_cached_status_always_displays_absolute_receipt_date_and_time_in_minsk(
    received: datetime, current: datetime, expected: str
) -> None:
    status = CabinetStatus(
        Decimal("1"),
        Decimal("247.27"),
        Decimal("252.73"),
        received,
    )
    for telegram_html in (False, True):
        text = format_cached_status(status, telegram_html=telegram_html, now=current)
        assert text == (
            format_status(status, telegram_html=telegram_html)
            + f"\n\n🕒 Данные получены {expected} (Минск)"
        )
        assert "сегодня" not in text
        assert "вчера" not in text


def test_empty_cache_shows_next_future_slot_after_minsk_midnight() -> None:
    text = format_empty_cache(now=datetime(2026, 10, 14, 21, 30, tzinfo=UTC))
    assert "15.10.2026 в 08:00 (Минск)" in text
    assert "08:00 и 22:00" in text
