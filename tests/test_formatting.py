from datetime import UTC, datetime
from decimal import Decimal

import pytest

from gippo_bot.cabinet import CabinetStatus
from gippo_bot.formatting import format_status


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
