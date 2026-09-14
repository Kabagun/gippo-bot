"""User-facing formatting for cabinet values."""

from __future__ import annotations

from decimal import Decimal

from .cabinet import CabinetStatus

_THREE_PERCENT_THRESHOLD = Decimal("500")
_FIVE_PERCENT_THRESHOLD = Decimal("1000")


def _format_decimal(value: Decimal, places: int, *, trim_trailing: bool = False) -> str:
    rendered = f"{value:.{places}f}".replace(".", ",")
    if trim_trailing:
        return rendered.rstrip("0").rstrip(",")
    if places == 2:
        whole, fraction = rendered.split(",")
        whole = f"{int(whole):,}".replace(",", " ")
        return f"{whole},{fraction}"
    return rendered


def _projected_discount_percent(monthly_purchases: Decimal) -> Decimal:
    """Return the next month's projected discount for this month's purchases."""

    if monthly_purchases >= _FIVE_PERCENT_THRESHOLD:
        return Decimal("5")
    if monthly_purchases >= _THREE_PERCENT_THRESHOLD:
        return Decimal("3")
    return Decimal("1")


def format_status(status: CabinetStatus) -> str:
    """Format the cabinet status as a concise Russian Telegram message."""

    projected_discount = _projected_discount_percent(status.monthly_purchases)
    lines = [
        f"Ваша текущая скидка: {_format_decimal(status.discount_percent, 2, trim_trailing=True)}%",
        "Прогноз скидки на следующий месяц: "
        f"{_format_decimal(projected_discount, 2, trim_trailing=True)}%",
        f"Покупки в этом месяце: {_format_decimal(status.monthly_purchases, 2)} руб.",
    ]
    if status.until_next_level > 0:
        lines.append(
            f"До следующего уровня: {_format_decimal(status.until_next_level, 2)} руб.",
        )
    return "\n".join(lines)
