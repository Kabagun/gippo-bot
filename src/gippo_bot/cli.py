"""Cabinet-only health check that does not require a Telegram token."""

from __future__ import annotations

import argparse
import json

from .cabinet import GippoClient, GippoError
from .config import CabinetSettings, SettingsError
from .formatting import format_status


def main() -> None:
    """Fetch and print the current cabinet values."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--sms-code",
        action="store_true",
        help="prompt privately for the code of a pending SMS login",
    )
    args = parser.parse_args()

    try:
        settings = CabinetSettings.from_environment()
        with GippoClient(
            settings.login,
            settings.password,
            **settings.client_options(),
        ) as client:
            if args.sms_code:
                from getpass import getpass

                status = client.submit_sms_code(getpass("GIPPO SMS code: ").strip())
            else:
                status = client.fetch_status()
    except (SettingsError, GippoError) as exc:
        raise SystemExit(str(exc)) from exc

    if args.json:
        print(
            json.dumps(
                {
                    "discount_percent": str(status.discount_percent),
                    "monthly_purchases": str(status.monthly_purchases),
                    "until_next_level": str(status.until_next_level),
                    "fetched_at": status.fetched_at.isoformat(),
                },
                ensure_ascii=False,
            )
        )
    else:
        print(format_status(status))


if __name__ == "__main__":
    main()
