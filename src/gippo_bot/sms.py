"""Client for the authenticated, consumer-scoped SMS challenge service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import httpx

CODE_PATTERN = r"(?<!\d)\d{4}(?!\d)"


class SmsServiceError(RuntimeError):
    """Raised when a challenge response is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class SmsChallenge:
    """A validated challenge result returned by the forwarding service."""

    id: str
    status: str
    expires_at: float | None = None
    code: str | None = None


class SmsForwarderClient:
    """Create and poll short-lived GIPPO challenges without waiting for an SMS."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        sender: str = "GIPPO",
        timeout_seconds: float = 5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._sender = sender
        self._client = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    def create(self, ttl_seconds: int) -> SmsChallenge:
        """Open a consumer challenge before GIPPO sends the matching SMS."""

        return self._request(
            "POST",
            "/internal/v1/challenges",
            json={"sender": self._sender, "ttl_seconds": ttl_seconds, "code_pattern": CODE_PATTERN},
            creating=True,
        )

    def poll(self, challenge_id: str) -> SmsChallenge:
        """Read one challenge immediately; this call never long-polls."""

        challenge = self._request("GET", self._path(challenge_id))
        if challenge.id != challenge_id:
            raise SmsServiceError("The SMS challenge response does not match")
        return challenge

    def delete(self, challenge_id: str) -> None:
        """Discard a challenge after successful login or expiry."""

        try:
            self._client.delete(self._path(challenge_id)).raise_for_status()
        except httpx.HTTPError as exc:
            raise SmsServiceError("The SMS challenge could not be cleared") from exc

    def close(self) -> None:
        """Close the service connection pool."""

        self._client.close()

    @staticmethod
    def _path(challenge_id: str) -> str:
        return f"/internal/v1/challenges/{quote(challenge_id, safe='')}"

    def _request(
        self, method: str, path: str, *, creating: bool = False, **kwargs: object
    ) -> SmsChallenge:
        try:
            response = self._client.request(method, path, **kwargs)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
            identifier = payload["id"]
            status = payload["status"]
            if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
                raise ValueError
            if status not in {"waiting", "received", "expired"}:
                raise ValueError
            code = payload.get("code")
            if status == "received" and (
                not isinstance(code, str) or not re.fullmatch(r"[0-9]{4}", code)
            ):
                raise ValueError
            if status != "received" and code is not None:
                raise ValueError
            expires_at = None
            if creating:
                if status != "waiting":
                    raise ValueError
                expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    raise ValueError
                expires_at = expires.timestamp()
            return SmsChallenge(identifier, status, expires_at, code)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise SmsServiceError(
                "The SMS service is unavailable or returned an invalid response"
            ) from exc
