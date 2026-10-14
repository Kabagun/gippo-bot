"""HTTP client and parser for the GIPPO loyalty cabinet."""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .session import ProtectedSessionStore, SessionStorageError
from .sms import SmsForwarderClient, SmsServiceError

DEFAULT_BASE_URL = "https://cabinet.gippo.by"


class GippoError(RuntimeError):
    """Base error raised by the GIPPO cabinet integration."""


class GippoAuthenticationError(GippoError):
    """Raised when the cabinet does not accept the configured credentials."""


class GippoPageError(GippoError):
    """Raised when the expected loyalty values cannot be read from the page."""


class GippoSmsRequired(GippoAuthenticationError):
    """A shared login is waiting for its existing SMS challenge."""

    def __init__(self, *, automatic: bool, expires_at: float) -> None:
        super().__init__("GIPPO login is waiting for the SMS confirmation code")
        self.automatic = automatic
        self.expires_at = expires_at


class GippoLoginThrottled(GippoAuthenticationError):
    """Another login cannot be attempted until the persistent cooldown ends."""

    def __init__(self, retry_at: float) -> None:
        super().__init__("GIPPO login is temporarily throttled")
        self.retry_at = retry_at


@dataclass(frozen=True, slots=True)
class CabinetStatus:
    """Loyalty values displayed by the GIPPO personal cabinet."""

    discount_percent: Decimal
    monthly_purchases: Decimal
    until_next_level: Decimal
    fetched_at: datetime


_NUMERIC_VALUE = r"([0-9][0-9\s.,]*)(?![0-9.,])"

_VALUE_PATTERNS = {
    "discount_percent": re.compile(
        rf"Ваша\s+текущая\s+скидка\s*{_NUMERIC_VALUE}\s*%",
        re.IGNORECASE,
    ),
    "monthly_purchases": re.compile(
        r"Ваши\s+покупки\s+в\s+этом\s+месяце\s*"
        rf"{_NUMERIC_VALUE}",
        re.IGNORECASE,
    ),
    "until_next_level": re.compile(
        r"До\s+следующего\s+уровня\s+осталось\s*"
        rf"{_NUMERIC_VALUE}",
        re.IGNORECASE,
    ),
}

_FIVE_PERCENT_THRESHOLD = Decimal("1000")


def _to_decimal(raw_value: str) -> Decimal:
    raw_value = raw_value.strip()
    if re.fullmatch(r"[0-9]{1,3}(?:\s+[0-9]{3})+(?:[.,][0-9]{1,2})?", raw_value):
        normalized = re.sub(r"\s+", "", raw_value).replace(",", ".")
    elif re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?", raw_value):
        normalized = raw_value.replace(",", "")
    elif re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{3})+(?:,[0-9]{1,2})?", raw_value):
        normalized = raw_value.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"[0-9]+(?:[.,][0-9]{1,2})?", raw_value):
        normalized = raw_value.replace(",", ".")
    else:
        raise GippoPageError("The cabinet returned an invalid numeric value")
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise GippoPageError("The cabinet returned an invalid numeric value") from exc


def parse_personal_page(html: str) -> CabinetStatus:
    """Extract loyalty values from an authenticated personal-cabinet page."""

    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    values: dict[str, Decimal] = {}

    for field in ("discount_percent", "monthly_purchases"):
        pattern = _VALUE_PATTERNS[field]
        match = pattern.search(text)
        if match is None:
            raise GippoPageError(f"The cabinet page does not contain {field}")
        values[field] = _to_decimal(match.group(1))

    until_next_level_match = _VALUE_PATTERNS["until_next_level"].search(text)
    if until_next_level_match is None:
        if values["monthly_purchases"] < _FIVE_PERCENT_THRESHOLD:
            raise GippoPageError("The cabinet page does not contain until_next_level")
        values["until_next_level"] = Decimal("0")
    else:
        values["until_next_level"] = _to_decimal(until_next_level_match.group(1))

    return CabinetStatus(
        discount_percent=values["discount_percent"],
        monthly_purchases=values["monthly_purchases"],
        until_next_level=values["until_next_level"],
        fetched_at=datetime.now(UTC),
    )


class GippoClient:
    """Share one persistent cabinet session and serialize all login attempts.

    SMS challenges are created before the upstream SMS request. A fetch polls
    an existing challenge once and returns promptly while it is waiting. An
    owner can complete that same challenge with :meth:`submit_sms_code`.
    """

    def __init__(
        self,
        login: str,
        password: str = "",
        *,
        auth_mode: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 20,
        session_path: Path | None = None,
        sms_forwarder_url: str = "http://127.0.0.1:18090",
        sms_forwarder_token: str = "",
        sms_sender: str = "GIPPO",
        sms_ttl_seconds: int = 300,
        sms_cooldown_seconds: int = 900,
        transport: httpx.BaseTransport | None = None,
        sms_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._login = login
        self._password = password
        self._auth_mode = auth_mode or ("password" if password else "sms")
        if self._auth_mode not in {"sms", "password"}:
            raise ValueError("GIPPO authentication mode must be sms or password")
        if not 60 <= sms_ttl_seconds <= 300 or sms_cooldown_seconds < 900:
            raise ValueError(
                "GIPPO SMS TTL must be 60–300 seconds and cooldown at least 900 seconds"
            )
        self._base_url = base_url.rstrip("/")
        self._scope = hashlib.sha256(
            f"{self._base_url}|{self._auth_mode}|{login}".encode()
        ).hexdigest()
        self._store = ProtectedSessionStore(session_path) if session_path else None
        self._sms_ttl = sms_ttl_seconds
        self._sms_cooldown = sms_cooldown_seconds
        self._sms = (
            SmsForwarderClient(
                sms_forwarder_url, sms_forwarder_token, sender=sms_sender, transport=sms_transport
            )
            if sms_forwarder_token
            else None
        )
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Accept-Language": "ru,en;q=0.9",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
                ),
            },
            timeout=timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )
        self._lock = threading.RLock()
        self._authenticated = False
        self._pending: dict[str, Any] | None = None
        self._next_login_at = 0.0
        self._failures = 0
        self._closed = False

    def fetch_status(self) -> CabinetStatus:
        """Fetch loyalty values or report a pending SMS/cooldown immediately.

        A saved session is checked before any login request, including when
        the SMS service is unavailable. Expired sessions start at most one
        challenge per persistent cooldown, with exponential failure backoff.
        """

        return self._operate(self._fetch_status)

    def submit_sms_code(self, code: str) -> CabinetStatus:
        """Confirm an existing SMS challenge; never send another SMS.

        The Telegram integration must authorize the owner before calling this
        method. A challenge permits at most three distinct code submissions.
        """

        if not re.fullmatch(r"[0-9]{4}", code):
            raise GippoAuthenticationError("The SMS code must contain exactly four digits")
        return self._operate(lambda: self._submit_code(code))

    def poll_sms_login(self) -> CabinetStatus:
        """Finish an existing SMS login without ever requesting another SMS.

        A recovered authenticated session is also accepted. The same persistent
        locks, challenge deadline, and code-attempt limits apply as for a status
        request, including when another process completed or cleared the login.
        """

        return self._operate(lambda: self._fetch_status(allow_login=False))

    def close(self) -> None:
        """Close connection pools idempotently and prohibit further requests."""

        with self._lock:
            if not self._closed:
                self._closed = True
                self._client.close()
                if self._sms:
                    self._sms.close()

    def __enter__(self) -> GippoClient:
        """Return the open client for use as a context manager."""

        self._ensure_open()
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        """Close connection pools on leaving a context manager."""

        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise GippoError("The GIPPO client is closed")

    def _operate(self, action) -> CabinetStatus:
        with self._lock:
            self._ensure_open()
            try:
                with self._store.operation() if self._store else nullcontext():
                    self._restore()
                    try:
                        return action()
                    finally:
                        self._save()
            except (SessionStorageError, OSError) as exc:
                raise GippoError("Could not access the protected GIPPO session") from exc
            except httpx.HTTPError as exc:
                raise GippoError("Could not communicate with the GIPPO cabinet") from exc

    def _fetch_status(self, *, allow_login: bool = True) -> CabinetStatus:
        # Pending cookies can already have been authenticated before a crash.
        if self._authenticated or self._client.cookies:
            response = self._get_personal()
            if not self._is_login_response(response):
                return self._accept_personal(response)
            self._authenticated = False
            if not self._pending:
                self._client.cookies.clear()

        if self._pending:
            return self._poll_pending()
        if not allow_login:
            raise GippoAuthenticationError("There is no pending GIPPO SMS challenge")
        if time.time() < self._next_login_at:
            raise GippoLoginThrottled(self._next_login_at)
        if self._auth_mode == "sms":
            self._start_sms()
            raise self._sms_required()

        self._reserve_attempt()
        payload = self._post_auth(
            {
                "PROP_RQ[LOGIN]": self._login,
                "PROP_RQ[PASSWORD]": self._password,
                "PROP[REMEMBER]": "Y",
            }
        )
        self._require_login_success(payload)
        return self._verify_login()

    def _reserve_attempt(self) -> None:
        self._failures = min(self._failures + 1, 8)
        self._next_login_at = time.time() + min(
            self._sms_cooldown * 2 ** (self._failures - 1), 86400
        )
        # Persist the reservation before any request with uncertain delivery.
        self._save()

    def _start_sms(self) -> None:
        self._reserve_attempt()
        challenge_id = None
        expires_at = time.time() + self._sms_ttl
        if self._sms:
            try:
                challenge = self._sms.create(self._sms_ttl)
            except SmsServiceError as exc:
                raise GippoAuthenticationError(
                    "The SMS receiver is unavailable; no SMS was sent"
                ) from exc
            if (
                challenge.expires_at is None
                or not time.time() < challenge.expires_at <= expires_at + 30
            ):
                self._clear_challenge(challenge.id)
                raise GippoAuthenticationError("The SMS receiver returned an invalid expiry")
            challenge_id = challenge.id
            expires_at = challenge.expires_at

        self._pending = {
            "challenge_id": challenge_id,
            "expires_at": expires_at,
            "phase": "sending",
            "last_poll_at": 0.0,
            "submitted": [],
        }
        self._save()
        try:
            payload = self._post_auth({"PROP[STEP]": "1", "PROP_RQ[PHONE]": self._login})
            if payload.get("available") not in (1, "1") or payload.get("step") not in (1, "1"):
                raise GippoAuthenticationError("GIPPO did not confirm sending the SMS")
        except GippoAuthenticationError:
            self._discard_pending()
            raise
        finally:
            # A timeout may have sent the SMS: retain cookies and the challenge.
            self._save()
        self._pending["phase"] = "waiting"

    def _poll_pending(self) -> CabinetStatus:
        if self._pending is None:
            raise GippoAuthenticationError("There is no pending GIPPO SMS challenge")
        if time.time() >= self._pending["expires_at"]:
            self._discard_pending()
            raise GippoLoginThrottled(self._next_login_at)
        identifier = self._pending["challenge_id"]
        if self._sms and identifier and time.time() >= self._pending["last_poll_at"] + 5:
            self._pending["last_poll_at"] = time.time()
            self._save()
            try:
                challenge = self._sms.poll(identifier)
            except SmsServiceError:
                raise self._sms_required() from None
            if challenge.status == "expired":
                self._discard_pending()
                raise GippoLoginThrottled(self._next_login_at)
            if challenge.status == "received" and challenge.code:
                digest = hashlib.sha256(challenge.code.encode()).hexdigest()
                if digest not in self._pending["submitted"]:
                    return self._submit_code(challenge.code)
        raise self._sms_required()

    def _sms_required(self) -> GippoSmsRequired:
        assert self._pending is not None
        return GippoSmsRequired(
            automatic=bool(self._sms and self._pending["challenge_id"]),
            expires_at=self._pending["expires_at"],
        )

    def _submit_code(self, code: str) -> CabinetStatus:
        if self._pending is None:
            raise GippoAuthenticationError("There is no pending GIPPO SMS challenge")
        if time.time() >= self._pending["expires_at"]:
            self._discard_pending()
            raise GippoLoginThrottled(self._next_login_at)
        digest = hashlib.sha256(code.encode()).hexdigest()
        submitted = self._pending["submitted"]
        if digest in submitted:
            raise GippoAuthenticationError("This SMS code was already submitted")
        if len(submitted) >= 3:
            self._discard_pending()
            raise GippoLoginThrottled(self._next_login_at)
        submitted.append(digest)
        self._save()
        payload = self._post_auth({"PROP[STEP]": "2", "PROP[SMS_CODE]": code})
        self._require_login_success(payload)
        return self._verify_login()

    def _verify_login(self) -> CabinetStatus:
        response = self._get_personal()
        if self._is_login_response(response):
            raise GippoAuthenticationError("GIPPO cabinet authentication was not accepted")
        return self._accept_personal(response)

    def _accept_personal(self, response: httpx.Response) -> CabinetStatus:
        status = parse_personal_page(response.text)
        self._authenticated = True
        self._failures = 0
        self._discard_pending()
        if self._auth_mode == "password":
            self._next_login_at = 0.0
        # Preserve the SMS cooldown even after success and immediate logout.
        return status

    def _discard_pending(self) -> None:
        if self._pending:
            self._clear_challenge(self._pending["challenge_id"])
        self._pending = None

    def _clear_challenge(self, identifier: str | None) -> None:
        if self._sms and identifier:
            # The service TTL clears an unavailable receiver safely.
            with suppress(SmsServiceError):
                self._sms.delete(identifier)

    def _post_auth(self, fields: dict[str, str]) -> dict[str, Any]:
        response = self._client.post(
            "/local/ajax/auth.php",
            files={name: (None, value) for name, value in fields.items()},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Origin": self._base_url,
                "Referer": f"{self._base_url}/index.php",
                "X-Requested-With": "XMLHttpRequest",
            },
            follow_redirects=False,
        )
        if response.status_code in {401, 403, 429}:
            raise GippoAuthenticationError("GIPPO cabinet authentication was not accepted")
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise GippoAuthenticationError("GIPPO returned an invalid login response") from exc
        if not isinstance(payload, dict) or payload.get("err") or payload.get("showError"):
            raise GippoAuthenticationError("GIPPO cabinet authentication was not accepted")
        return payload

    def _require_login_success(self, payload: dict[str, Any]) -> None:
        link = payload.get("link")
        internal_personal_link = isinstance(link, str) and (
            link in {"/personal", "/personal/"} or link.rstrip("/") == f"{self._base_url}/personal"
        )
        if payload.get("reload") not in (True, 1, "1") and not internal_personal_link:
            raise GippoAuthenticationError("GIPPO did not confirm authentication")

    def _get_personal(self) -> httpx.Response:
        response = self._client.get(
            "/personal/",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": f"{self._base_url}/index.php",
            },
        )
        if response.status_code not in {401, 403}:
            response.raise_for_status()
        return response

    def _is_login_response(self, response: httpx.Response) -> bool:
        expected = httpx.URL(self._base_url)
        return (
            response.status_code in {401, 403}
            or (response.url.scheme, response.url.host, response.url.port)
            != (expected.scheme, expected.host, expected.port)
            or response.url.path.rstrip("/") != "/personal"
            or bool(
                re.search(
                    r"(?:PROP_RQ\[(?:PASSWORD|PHONE)\]|PROP\[SMS_CODE\]|"
                    r"name\s*=\s*['\"]password['\"])",
                    response.text,
                    re.IGNORECASE,
                )
            )
        )

    def _save(self) -> None:
        if self._store:
            self._store.write(
                {
                    "version": 1,
                    "scope": self._scope,
                    "authenticated": self._authenticated,
                    "cookies": [vars(cookie) for cookie in self._client.cookies.jar],
                    "pending": self._pending,
                    "next_login_at": self._next_login_at,
                    "failures": self._failures,
                }
            )

    def _restore(self) -> None:
        if not self._store:
            return
        state = self._store.read()
        if state is None:
            return
        try:
            if state["version"] != 1 or state["scope"] != self._scope:
                raise ValueError
            if (
                not isinstance(state["next_login_at"], (int, float))
                or not math.isfinite(state["next_login_at"])
                or state["next_login_at"] < 0
                or not isinstance(state["failures"], int)
                or not 0 <= state["failures"] <= 8
                or not isinstance(state["authenticated"], bool)
                or not isinstance(state["cookies"], list)
            ):
                raise ValueError
            pending = state["pending"]
            if pending is not None and (
                not isinstance(pending, dict)
                or pending["phase"] not in {"sending", "waiting"}
                or not isinstance(pending["expires_at"], (int, float))
                or not math.isfinite(pending["expires_at"])
                or not isinstance(pending["last_poll_at"], (int, float))
                or not math.isfinite(pending["last_poll_at"])
                or not isinstance(pending["submitted"], list)
                or len(pending["submitted"]) > 3
                or not all(
                    isinstance(item, str) and re.fullmatch(r"[a-f0-9]{64}", item)
                    for item in pending["submitted"]
                )
                or (
                    pending["challenge_id"] is not None
                    and not isinstance(pending["challenge_id"], str)
                )
            ):
                raise ValueError
            cookies = httpx.Cookies()
            for raw_cookie in state["cookies"]:
                raw_cookie = dict(raw_cookie)
                raw_cookie["rest"] = raw_cookie.pop("_rest")
                cookies.jar.set_cookie(Cookie(**raw_cookie))
            self._client.cookies = cookies
            self._authenticated = bool(state["authenticated"])
            self._pending = pending
            self._next_login_at = state["next_login_at"]
            self._failures = state["failures"]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise SessionStorageError("The private GIPPO session state is invalid") from exc
