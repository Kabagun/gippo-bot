"""Environment-based application settings."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from .cabinet import DEFAULT_BASE_URL
from .session import SessionStorageError, read_protected_token

DEFAULT_GIPPO_CARD_IMAGE_PATH = Path("/srv/bots/gippo-bot/private/cards/gippo.png")
DEFAULT_BELMARKET_CARD_IMAGE_PATH = Path("/srv/bots/gippo-bot/private/cards/belmarket.png")


class SettingsError(ValueError):
    """Raised when required application configuration is missing or invalid."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SettingsError(f"Required environment variable is missing: {name}")
    return value


def _allowed_user_ids(raw_value: str) -> frozenset[int]:
    values = [part.strip() for part in raw_value.replace(";", ",").split(",")]
    try:
        result = frozenset(int(value) for value in values if value)
    except ValueError as exc:
        raise SettingsError("TELEGRAM_ALLOWED_USER_IDS must contain numeric IDs") from exc
    return result


def _boolean(name: str, *, default: bool = False) -> bool:
    raw_value = os.getenv(name, str(default)).strip().lower()
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class CabinetSettings:
    """Configuration required to query the cabinet without Telegram."""

    login: str
    password: str = field(default="", repr=False)
    auth_mode: str = "sms"
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 20
    session_path: Path | None = None
    sms_forwarder_url: str = "http://127.0.0.1:18090"
    sms_forwarder_token: str = field(default="", repr=False)
    sms_sender: str = "GIPPO"
    sms_ttl_seconds: int = 300
    sms_cooldown_seconds: int = 900

    @classmethod
    def from_environment(cls) -> CabinetSettings:
        """Load and validate cabinet settings from process environment variables."""

        try:
            timeout_seconds = float(os.getenv("GIPPO_TIMEOUT_SECONDS", "20"))
        except ValueError as exc:
            raise SettingsError("GIPPO_TIMEOUT_SECONDS must be a number") from exc
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise SettingsError("GIPPO_TIMEOUT_SECONDS must be greater than zero")

        auth_mode = os.getenv("GIPPO_AUTH_MODE", "sms").strip().lower()
        if auth_mode not in {"sms", "password"}:
            raise SettingsError("GIPPO_AUTH_MODE must be sms or password")
        login = _required("GIPPO_PHONE" if auth_mode == "sms" else "GIPPO_LOGIN")
        if auth_mode == "sms":
            digits = "".join(character for character in login if character.isdigit())
            if len(digits) != 12 or not digits.startswith("375"):
                raise SettingsError(
                    "GIPPO_PHONE must be a Belarusian phone number beginning with +375"
                )
            login = "+" + digits

        try:
            sms_ttl = int(os.getenv("GIPPO_SMS_TTL_SECONDS", "300"))
            sms_cooldown = int(os.getenv("GIPPO_SMS_COOLDOWN_SECONDS", "900"))
        except ValueError as exc:
            raise SettingsError("GIPPO SMS time limits must be integer seconds") from exc
        if not 60 <= sms_ttl <= 300 or sms_cooldown < 900:
            raise SettingsError(
                "GIPPO SMS TTL must be 60–300 seconds and cooldown at least 900 seconds"
            )

        session_path = os.getenv("GIPPO_SESSION_PATH", "").strip()
        if auth_mode == "sms" and not session_path:
            raise SettingsError("Required environment variable is missing: GIPPO_SESSION_PATH")

        sms_token = os.getenv("SMS_FORWARDER_TOKEN", "").strip()
        sms_token_file = os.getenv("SMS_FORWARDER_TOKEN_FILE", "").strip()
        if sms_token and sms_token_file:
            raise SettingsError("Set only one of SMS_FORWARDER_TOKEN and SMS_FORWARDER_TOKEN_FILE")
        if sms_token_file:
            try:
                sms_token = read_protected_token(Path(sms_token_file).expanduser())
            except SessionStorageError as exc:
                raise SettingsError(
                    "SMS_FORWARDER_TOKEN_FILE must be a readable protected token file"
                ) from exc
        sms_sender = os.getenv("GIPPO_SMS_SENDER", "GIPPO").strip()
        if not sms_sender or len(sms_sender) > 100 or any(ord(char) < 32 for char in sms_sender):
            raise SettingsError("GIPPO_SMS_SENDER must contain a valid SMS sender name")

        return cls(
            login=login,
            password=_required("GIPPO_PASSWORD") if auth_mode == "password" else "",
            auth_mode=auth_mode,
            base_url=os.getenv("GIPPO_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/"),
            timeout_seconds=timeout_seconds,
            session_path=Path(session_path).expanduser() if session_path else None,
            sms_forwarder_url=os.getenv("SMS_FORWARDER_URL", "http://127.0.0.1:18090").strip(),
            sms_forwarder_token=sms_token,
            sms_sender=sms_sender,
            sms_ttl_seconds=sms_ttl,
            sms_cooldown_seconds=sms_cooldown,
        )

    def client_options(self) -> dict[str, object]:
        """Return the cabinet client's keyword options for bot and CLI callers."""

        return {
            "auth_mode": self.auth_mode,
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
            "session_path": self.session_path,
            "sms_forwarder_url": self.sms_forwarder_url,
            "sms_forwarder_token": self.sms_forwarder_token,
            "sms_sender": self.sms_sender,
            "sms_ttl_seconds": self.sms_ttl_seconds,
            "sms_cooldown_seconds": self.sms_cooldown_seconds,
        }


@dataclass(frozen=True, slots=True)
class BotSettings:
    """Complete Telegram bot configuration."""

    token: str = field(repr=False)
    open_access: bool
    allowed_user_ids: frozenset[int]
    cabinet: CabinetSettings
    log_level: str = "INFO"
    gippo_card_image_path: Path = DEFAULT_GIPPO_CARD_IMAGE_PATH
    belmarket_card_image_path: Path = DEFAULT_BELMARKET_CARD_IMAGE_PATH
    owner_user_id: int | None = None

    @classmethod
    def from_environment(cls) -> BotSettings:
        """Load and validate all bot settings from process environment variables."""

        open_access = _boolean("TELEGRAM_OPEN_ACCESS")
        allowed_user_ids = _allowed_user_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""))
        if not open_access and not allowed_user_ids:
            raise SettingsError(
                "Set TELEGRAM_OPEN_ACCESS=true or provide TELEGRAM_ALLOWED_USER_IDS"
            )

        owner_raw = os.getenv("TELEGRAM_OWNER_USER_ID", "").strip()
        try:
            owner_user_id = int(owner_raw) if owner_raw else None
        except ValueError as exc:
            raise SettingsError("TELEGRAM_OWNER_USER_ID must be a numeric user ID") from exc
        if owner_user_id is not None and owner_user_id <= 0:
            raise SettingsError("TELEGRAM_OWNER_USER_ID must be a positive user ID")
        cabinet = CabinetSettings.from_environment()
        if cabinet.auth_mode == "sms" and not cabinet.sms_forwarder_token and not owner_user_id:
            raise SettingsError("SMS login requires SMS_FORWARDER_TOKEN or TELEGRAM_OWNER_USER_ID")

        return cls(
            token=_required("TELEGRAM_BOT_TOKEN"),
            open_access=open_access,
            allowed_user_ids=allowed_user_ids,
            cabinet=cabinet,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            gippo_card_image_path=Path(
                os.getenv("GIPPO_CARD_IMAGE_PATH", str(DEFAULT_GIPPO_CARD_IMAGE_PATH))
            ).expanduser(),
            belmarket_card_image_path=Path(
                os.getenv("BELMARKET_CARD_IMAGE_PATH", str(DEFAULT_BELMARKET_CARD_IMAGE_PATH))
            ).expanduser(),
            owner_user_id=owner_user_id,
        )
