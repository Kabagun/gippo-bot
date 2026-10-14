"""Read saved cabinet status without triggering a provider refresh."""

from __future__ import annotations

import argparse
import json

from .cabinet import GippoError
from .cache import CacheCycleUnavailable, CacheError, next_update, utc_now
from .config import CabinetSettings, SettingsError
from .formatting import format_cached_status, format_empty_cache


def main() -> None:
    """Print cached values or confirm SMS only during an active scheduled cycle."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--sms-code",
        action="store_true",
        help="confirm SMS only during an active scheduled update",
    )
    args = parser.parse_args()

    try:
        settings = CabinetSettings.from_environment()
        cache = settings.status_cache()
        if args.sms_code:
            if not cache.has_active_sms():
                raise CacheCycleUnavailable("There is no active scheduled GIPPO SMS update")
            from getpass import getpass

            status = cache.submit_sms_code(
                settings.create_client, getpass("GIPPO SMS code: ").strip()
            )
        else:
            status = cache.read_status()
    except (SettingsError, CacheError, GippoError) as exc:
        raise SystemExit(str(exc)) from exc

    if args.json:
        print(
            json.dumps(
                {
                    "discount_percent": str(status.discount_percent),
                    "monthly_purchases": str(status.monthly_purchases),
                    "until_next_level": str(status.until_next_level),
                    "fetched_at": status.fetched_at.isoformat(),
                }
                if status
                else {"status": None, "next_update": next_update(utc_now()).isoformat()},
                ensure_ascii=False,
            )
        )
    else:
        print(format_cached_status(status) if status else format_empty_cache())


if __name__ == "__main__":
    main()
