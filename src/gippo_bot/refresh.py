"""Bounded cache refresh invoked by the twice-daily systemd timer."""

from __future__ import annotations

import logging

from .cache import CacheError
from .config import CabinetSettings, SettingsError


def main() -> None:
    """Refresh once within a scheduled window; skip duplicate or late invocations."""

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = CabinetSettings.from_environment()
        result = settings.status_cache().run_scheduled(settings.create_client)
    except (SettingsError, CacheError) as exc:
        raise SystemExit(str(exc)) from exc
    logging.getLogger(__name__).info("Scheduled GIPPO cache refresh: %s", result.outcome)
    if result.outcome == "failed":
        raise SystemExit("The scheduled refresh failed; the previous snapshot was retained")


if __name__ == "__main__":
    main()
