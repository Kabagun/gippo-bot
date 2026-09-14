"""HTTP client and parser for the GIPPO loyalty cabinet."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx
from bs4 import BeautifulSoup

DEFAULT_BASE_URL = "https://cabinet.gippo.by"


class GippoError(RuntimeError):
    """Base error raised by the GIPPO cabinet integration."""


class GippoAuthenticationError(GippoError):
    """Raised when the cabinet does not accept the configured credentials."""


class GippoPageError(GippoError):
    """Raised when the expected loyalty values cannot be read from the page."""


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
    """Authenticate to the GIPPO cabinet and fetch the current loyalty status.

    A client owns one HTTP session for its entire lifetime.  The session cookie
    is therefore shared by all callers (the Telegram bot deliberately creates
    one client for all users), while a lock serializes access because
    :class:`httpx.Client` is a synchronous, mutable session object.
    """

    def __init__(
        self,
        login: str,
        password: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 20,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._login = login
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Accept-Language": "ru,en;q=0.9",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
                ),
            },
            timeout=self._timeout_seconds,
            follow_redirects=True,
            transport=self._transport,
        )
        self._lock = threading.RLock()
        self._authenticated = False
        self._closed = False

    def fetch_status(self) -> CabinetStatus:
        """Return the three loyalty values from ``/personal/``.

        The first call authenticates the session.  Later calls reuse its
        cookie, and a redirect to the login page (or an expired-session
        response) triggers one re-authentication before the page is retried.
        Calls are serialized so concurrent bot updates cannot race on the
        shared session or perform duplicate logins.
        """

        with self._lock:
            self._ensure_open()
            try:
                return self._fetch_status()
            except GippoError:
                raise
            except httpx.HTTPError as exc:
                raise GippoError("Could not communicate with the GIPPO cabinet") from exc

    def close(self) -> None:
        """Close the underlying HTTP session and release its resources.

        Closing is idempotent.  A client cannot be used for another fetch once
        it has been closed.
        """

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._client.close()

    def __enter__(self) -> GippoClient:
        """Return this client for use as a context manager."""

        with self._lock:
            self._ensure_open()
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        """Close the HTTP session when leaving a context manager."""

        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise GippoError("The GIPPO client is closed")

    def _fetch_status(self) -> CabinetStatus:
        authenticated_for_this_fetch = False
        if not self._authenticated:
            self._authenticate()
            authenticated_for_this_fetch = True

        personal_response = self._get_personal()
        if self._is_login_response(personal_response):
            self._authenticated = False
            self._client.cookies.clear()
            if authenticated_for_this_fetch:
                raise GippoAuthenticationError("GIPPO cabinet authentication was not accepted")

            self._authenticate()
            personal_response = self._get_personal()
            if self._is_login_response(personal_response):
                self._authenticated = False
                self._client.cookies.clear()
                raise GippoAuthenticationError("GIPPO cabinet authentication was not accepted")

        personal_response.raise_for_status()
        return parse_personal_page(personal_response.text)

    def _authenticate(self) -> None:
        auth_response = self._client.post(
            "/local/ajax/auth.php",
            files={
                "PROP_RQ[LOGIN]": (None, self._login),
                "PROP_RQ[PASSWORD]": (None, self._password),
                "PROP[REMEMBER]": (None, "Y"),
            },
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Origin": self._base_url,
                "Referer": f"{self._base_url}/index.php",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if auth_response.status_code in {401, 403}:
            raise GippoAuthenticationError("GIPPO cabinet authentication was not accepted")
        auth_response.raise_for_status()
        self._authenticated = True

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

    @staticmethod
    def _is_login_response(response: httpx.Response) -> bool:
        """Return whether a personal-page response indicates an expired login."""

        if response.status_code in {401, 403}:
            return True
        if "/personal" not in response.url.path:
            return True
        return bool(
            re.search(
                r"(?:PROP_RQ\[PASSWORD\]|name\s*=\s*['\"]password['\"])",
                response.text,
                re.IGNORECASE,
            )
        )
