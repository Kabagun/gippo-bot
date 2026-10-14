import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from gippo_bot.cabinet import (
    GippoAuthenticationError,
    GippoClient,
    GippoError,
    GippoLoginThrottled,
    GippoPageError,
    GippoSmsRequired,
)

PAGE = (Path(__file__).parent / "fixtures" / "personal.html").read_text(encoding="utf-8")
LOGIN_PAGE = '<form><input name="PROP_RQ[PHONE]"></form>'


class CabinetServer:
    def __init__(self) -> None:
        self.sms_requests = 0
        self.code_requests = 0
        self.personal_requests = 0
        self.authenticated = False
        self.step1_payload: object = {"available": 1, "step": 1}
        self.step2_payload: object = {"reload": True}
        self.personal_page = PAGE
        self.timeout_on_sms = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/personal/":
            self.personal_requests += 1
            assert "PHPSESSID=" in request.headers.get("cookie", "")
            return httpx.Response(
                200, text=self.personal_page if self.authenticated else LOGIN_PAGE
            )
        assert request.method == "POST"
        assert request.url.path == "/local/ajax/auth.php"
        assert request.headers["x-requested-with"] == "XMLHttpRequest"
        assert "multipart/form-data" in request.headers["content-type"]
        content = request.content.decode()
        assert 'name="PROP[STEP]"' in content
        assert 'name="PROP_RQ[PASSWORD]"' not in content
        if 'name="PROP_RQ[PHONE]"' in content:
            assert "+375291234567" in content
            assert "\r\n1\r\n" in content
            self.sms_requests += 1
            if self.timeout_on_sms:
                raise httpx.ReadTimeout("ambiguous SMS send", request=request)
            return httpx.Response(
                200, json=self.step1_payload, headers={"set-cookie": "PHPSESSID=pending; Path=/"}
            )
        assert 'name="PROP[SMS_CODE]"' in content
        assert "\r\n2\r\n" in content
        assert "PHPSESSID=pending" in request.headers["cookie"]
        self.code_requests += 1
        self.authenticated = bool(
            isinstance(self.step2_payload, dict) and self.step2_payload.get("reload")
        )
        return httpx.Response(200, json=self.step2_payload)


class ForwarderServer:
    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.calls: list[tuple[str, str]] = []
        self.status = "waiting"
        self.create_status = 200
        self.poll_status = 200
        self.code = "12345"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer consumer-token"
        self.calls.append((request.method, request.url.path))
        if request.method == "POST":
            assert request.url.path == "/internal/v1/challenges"
            assert json.loads(request.content) == {
                "sender": "GIPPO",
                "ttl_seconds": 300,
                "code_pattern": r"(?<!\d)\d{5}(?!\d)",
            }
            return httpx.Response(
                self.create_status,
                json={
                    "id": "challenge-1",
                    "status": "waiting",
                    "expires_at": datetime.fromtimestamp(self.clock[0] + 300, UTC).isoformat(),
                },
            )
        assert request.url.path == "/internal/v1/challenges/challenge-1"
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(
            self.poll_status,
            json={
                "id": "challenge-1",
                "status": self.status,
                "code": self.code if self.status == "received" else None,
            },
        )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    value = [1_700_000_000.0]
    monkeypatch.setattr("gippo_bot.cabinet.time.time", lambda: value[0])
    return value


def client(
    server: CabinetServer, path: Path, forwarder: ForwarderServer | None = None
) -> GippoClient:
    return GippoClient(
        "+375291234567",
        auth_mode="sms",
        session_path=path,
        transport=httpx.MockTransport(server),
        sms_forwarder_token="consumer-token" if forwarder else "",
        sms_transport=httpx.MockTransport(forwarder) if forwarder else None,
    )


def test_manual_login_persists_pending_cookies_and_authenticated_session(
    tmp_path: Path, clock: list[float]
) -> None:
    server = CabinetServer()
    path = tmp_path / "session.json"
    with client(server, path) as first:
        with pytest.raises(GippoSmsRequired) as pending:
            first.fetch_status()
        assert not pending.value.automatic
        assert pending.value.expires_at == clock[0] + 300
    assert server.sms_requests == 1
    with client(server, path) as restarted:
        with pytest.raises(GippoSmsRequired):
            restarted.fetch_status()
        status = restarted.submit_sms_code("12345")
        assert str(status.monthly_purchases) == "247.27"
    with client(server, path) as restarted_again:
        restarted_again.fetch_status()
    assert server.sms_requests == server.code_requests == 1
    state = json.loads(path.read_text())
    assert state["authenticated"] is True
    assert state["pending"] is None
    assert "+375291234567" not in path.read_text()
    assert "12345" not in path.read_text()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_automatic_challenge_precedes_sms_and_poll_returns_promptly(
    tmp_path: Path, clock: list[float]
) -> None:
    server = CabinetServer()
    forwarder = ForwarderServer(clock)
    with client(server, tmp_path / "session.json", forwarder) as instance:
        with pytest.raises(GippoSmsRequired) as pending:
            instance.fetch_status()
        assert pending.value.automatic
        assert forwarder.calls == [("POST", "/internal/v1/challenges")]
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        assert forwarder.calls.count(("GET", "/internal/v1/challenges/challenge-1")) == 1
        forwarder.status = "received"
        clock[0] += 5
        instance.fetch_status()
    assert server.sms_requests == server.code_requests == 1
    assert forwarder.calls[-1] == ("DELETE", "/internal/v1/challenges/challenge-1")


def test_profile_phone_form_does_not_repeat_sms_after_saved_login(
    tmp_path: Path, clock: list[float]
) -> None:
    server = CabinetServer()
    server.personal_page = PAGE + '<form><input name="PROP_RQ[PHONE]"></form>'
    path = tmp_path / "session.json"
    forwarder = ForwarderServer(clock)
    with client(server, path, forwarder) as instance:
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        forwarder.status = "received"
        clock[0] += 5
        status = instance.poll_sms_login()
        assert str(status.monthly_purchases) == "247.27"
    with client(server, path, forwarder) as restarted:
        status = restarted.fetch_status()
        assert str(status.monthly_purchases) == "247.27"
    assert server.sms_requests == server.code_requests == 1
    assert forwarder.calls.count(("POST", "/internal/v1/challenges")) == 1


def test_pending_forwarder_challenge_survives_restart(tmp_path: Path, clock: list[float]) -> None:
    server = CabinetServer()
    forwarder = ForwarderServer(clock)
    path = tmp_path / "session.json"
    with client(server, path, forwarder) as initial, pytest.raises(GippoSmsRequired):
        initial.fetch_status()
    forwarder.status = "received"
    with client(server, path, forwarder) as restarted:
        restarted.fetch_status()
    assert forwarder.calls.count(("POST", "/internal/v1/challenges")) == 1
    assert server.sms_requests == server.code_requests == 1


def test_background_poll_never_starts_a_login_without_pending_state(
    tmp_path: Path, clock: list[float]
) -> None:
    server = CabinetServer()
    forwarder = ForwarderServer(clock)
    with (
        client(server, tmp_path / "session.json", forwarder) as instance,
        pytest.raises(GippoAuthenticationError, match="no pending"),
    ):
        instance.poll_sms_login()
    assert server.sms_requests == server.code_requests == 0
    assert forwarder.calls == []


def test_background_poll_completes_existing_login_and_never_resends_after_expiry(
    tmp_path: Path, clock: list[float]
) -> None:
    server = CabinetServer()
    forwarder = ForwarderServer(clock)
    path = tmp_path / "session.json"
    with client(server, path, forwarder) as instance:
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        with pytest.raises(GippoSmsRequired):
            instance.poll_sms_login()
        forwarder.status = "received"
        clock[0] += 5
        status = instance.poll_sms_login()
        assert str(status.monthly_purchases) == "247.27"
        # Another caller may log out after successful confirmation and after
        # the cooldown; the background path must still never send a new SMS.
        server.authenticated = False
        clock[0] += 900
        with pytest.raises(GippoAuthenticationError, match="no pending"):
            instance.poll_sms_login()
    assert server.sms_requests == server.code_requests == 1


def test_waiting_sms_polls_only_local_forwarder_until_code_arrives(tmp_path, clock) -> None:
    server = CabinetServer()
    forwarder = ForwarderServer(clock)
    with client(server, tmp_path / "session.json", forwarder) as instance:
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        for _ in range(10):
            clock[0] += 5
            with pytest.raises(GippoSmsRequired):
                instance.poll_sms_login()
        assert server.personal_requests == server.code_requests == 0
        assert server.sms_requests == 1
        forwarder.status = "received"
        clock[0] += 5
        instance.poll_sms_login()
    assert server.personal_requests == server.code_requests == server.sms_requests == 1


def test_definitely_rejected_manual_code_does_not_poll_cabinet_page(tmp_path, clock) -> None:
    server = CabinetServer()
    server.step2_payload = {"err": ["PROP[SMS_CODE]"]}
    forwarder = ForwarderServer(clock)
    path = tmp_path / "session.json"
    with client(server, path, forwarder) as instance:
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        with pytest.raises(GippoAuthenticationError):
            instance.submit_sms_code("12345")
    with client(server, path, forwarder) as restarted:
        for _ in range(10):
            clock[0] += 5
            with pytest.raises(GippoSmsRequired):
                restarted.poll_sms_login()
    assert server.personal_requests == 0
    assert server.code_requests == server.sms_requests == 1


def test_uncertain_submitted_code_recovers_authenticated_session(tmp_path, clock) -> None:
    server = CabinetServer()
    forwarder = ForwarderServer(clock)
    path = tmp_path / "session.json"
    with client(server, path, forwarder) as instance, pytest.raises(GippoSmsRequired):
        instance.fetch_status()
    # A process stopped after sending a code but before receiving confirmation.
    state = json.loads(path.read_text())
    state["pending"]["verify_pending"] = True
    path.write_text(json.dumps(state))
    server.authenticated = True
    with client(server, path, forwarder) as restarted:
        restarted.poll_sms_login()
    assert server.personal_requests == server.sms_requests == 1
    assert server.code_requests == 0


@pytest.mark.parametrize("failure", ["timeout", "unknown_response"])
def test_uncertain_code_response_keeps_verification_needed_across_restart(
    tmp_path, clock, failure
) -> None:
    server = CabinetServer()
    path = tmp_path / "session.json"

    def handler(request):
        response = server(request)
        if 'name="PROP[SMS_CODE]"' in request.content.decode():
            if failure == "timeout":
                raise httpx.ReadTimeout("response lost after acceptance", request=request)
            return httpx.Response(200, text="unrecognized success response")
        return response

    with GippoClient(
        "+375291234567", auth_mode="sms", session_path=path, transport=httpx.MockTransport(handler)
    ) as instance:
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        with pytest.raises(GippoError):
            instance.submit_sms_code("12345")
    assert json.loads(path.read_text())["pending"]["verify_pending"] is True
    with client(server, path) as restarted:
        restarted.poll_sms_login()
    assert server.personal_requests == server.code_requests == server.sms_requests == 1


def test_next_scheduled_fetch_discards_old_expired_challenge(tmp_path, clock) -> None:
    server = CabinetServer()
    forwarder = ForwarderServer(clock)
    path = tmp_path / "session.json"
    with client(server, path, forwarder) as morning, pytest.raises(GippoSmsRequired):
        morning.fetch_status()
    clock[0] += 14 * 3600
    with client(server, path, forwarder) as evening, pytest.raises(GippoSmsRequired):
        evening.fetch_status()
    assert server.sms_requests == 2
    assert forwarder.calls.count(("POST", "/internal/v1/challenges")) == 2


def test_background_poll_discards_expired_challenge_without_new_sms(
    tmp_path: Path, clock: list[float]
) -> None:
    server = CabinetServer()
    forwarder = ForwarderServer(clock)
    with client(server, tmp_path / "session.json", forwarder) as instance:
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        clock[0] += 1800
        with pytest.raises(GippoLoginThrottled):
            instance.poll_sms_login()
        with pytest.raises(GippoAuthenticationError, match="no pending"):
            instance.poll_sms_login()
    assert server.sms_requests == 1
    assert server.code_requests == 0


@pytest.mark.parametrize("create_status", [401, 409, 429, 503])
def test_unavailable_receiver_suppresses_sms_and_persists_backoff(
    tmp_path: Path, clock: list[float], create_status: int
) -> None:
    server = CabinetServer()
    forwarder = ForwarderServer(clock)
    forwarder.create_status = create_status
    path = tmp_path / "session.json"
    with (
        client(server, path, forwarder) as initial,
        pytest.raises(GippoAuthenticationError, match="no SMS was sent"),
    ):
        initial.fetch_status()
    with client(server, path, forwarder) as restarted:
        with pytest.raises(GippoLoginThrottled):
            restarted.fetch_status()
        clock[0] += 900
        with pytest.raises(GippoAuthenticationError):
            restarted.fetch_status()
        with pytest.raises(GippoLoginThrottled) as throttled:
            restarted.fetch_status()
        assert throttled.value.retry_at == clock[0] + 1800
    assert server.sms_requests == 0
    assert len(forwarder.calls) == 2


def test_successful_sms_login_caps_long_backoff_at_base_cooldown(
    tmp_path: Path, clock: list[float]
) -> None:
    server = CabinetServer()
    forwarder = ForwarderServer(clock)
    forwarder.create_status = 503
    path = tmp_path / "session.json"
    with client(server, path, forwarder) as instance:
        for attempt in range(7):
            with pytest.raises(GippoAuthenticationError, match="no SMS was sent"):
                instance.fetch_status()
            state = json.loads(path.read_text(encoding="utf-8"))
            assert state["failures"] == attempt + 1
            if attempt < 6:
                clock[0] = state["next_login_at"]

        long_retry_at = state["next_login_at"]
        assert long_retry_at == clock[0] + 16 * 60 * 60

        clock[0] = long_retry_at
        forwarder.create_status = 200
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        assert server.sms_requests == 1

        forwarder.status = "received"
        clock[0] += 5
        status = instance.fetch_status()
        assert str(status.monthly_purchases) == "247.27"
        success_at = clock[0]

        state = json.loads(path.read_text(encoding="utf-8"))
        assert state["failures"] == 0
        assert state["next_login_at"] <= success_at + 900

        retry_at = state["next_login_at"]
        clock[0] += 60
        instance.fetch_status()
        assert json.loads(path.read_text(encoding="utf-8"))["next_login_at"] == retry_at

        # The provider session expires immediately, but its base SMS cooldown
        # still applies until the next login attempt is allowed.
        server.authenticated = False
        with pytest.raises(GippoLoginThrottled) as throttled:
            instance.fetch_status()
        assert throttled.value.retry_at == success_at + 900

        # Once the base cooldown passes, the expired session can request one
        # new SMS instead of retaining the previous long backoff.
        clock[0] = success_at + 900
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        assert server.sms_requests == 2


def test_sms_send_timeout_never_resends_after_restart(tmp_path: Path, clock: list[float]) -> None:
    server = CabinetServer()
    server.timeout_on_sms = True
    path = tmp_path / "session.json"
    with client(server, path) as initial, pytest.raises(GippoError, match="communicate"):
        initial.fetch_status()
    with client(server, path) as restarted:
        with pytest.raises(GippoSmsRequired):
            restarted.fetch_status()
        clock[0] += 301
        with pytest.raises(GippoLoginThrottled):
            restarted.fetch_status()
    assert server.sms_requests == 1


@pytest.mark.parametrize(
    "payload",
    [{}, {"available": 0, "step": 1}, {"available": 1, "step": 2}, {"err": ["PROP_RQ[PHONE]"]}],
)
def test_step1_requires_positive_expected_step_response(
    tmp_path: Path, clock: list[float], payload: object
) -> None:
    server = CabinetServer()
    server.step1_payload = payload
    with client(server, tmp_path / "session.json") as instance:
        with pytest.raises(GippoAuthenticationError):
            instance.fetch_status()
        with pytest.raises(GippoLoginThrottled):
            instance.fetch_status()
    assert server.sms_requests == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"reload": False},
        {"err": ["PROP[SMS_CODE]"]},
        {"link": "https://other.example/personal/"},
    ],
)
def test_code_response_must_confirm_authentication(
    tmp_path: Path, clock: list[float], payload: object
) -> None:
    server = CabinetServer()
    server.step2_payload = payload
    with client(server, tmp_path / "session.json") as instance:
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        with pytest.raises(GippoAuthenticationError):
            instance.submit_sms_code("12345")
    assert server.personal_requests == 0


def test_success_response_also_requires_authenticated_personal_page(
    tmp_path: Path, clock: list[float]
) -> None:
    server = CabinetServer()
    server.personal_page = LOGIN_PAGE
    with client(server, tmp_path / "session.json") as instance:
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        with pytest.raises(GippoAuthenticationError, match="not accepted"):
            instance.submit_sms_code("12345")


def test_changed_personal_page_does_not_start_more_sms(tmp_path: Path, clock: list[float]) -> None:
    server = CabinetServer()
    path = tmp_path / "session.json"
    with client(server, path) as instance:
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        instance.submit_sms_code("12345")
        server.personal_page = "New layout with no values"
        with pytest.raises(GippoPageError):
            instance.fetch_status()
    assert server.sms_requests == 1


def test_existing_cookie_is_checked_before_forwarder_and_recovers_crash(
    tmp_path: Path, clock: list[float]
) -> None:
    server = CabinetServer()
    forwarder = ForwarderServer(clock)
    path = tmp_path / "session.json"
    with client(server, path, forwarder) as instance, pytest.raises(GippoSmsRequired):
        instance.fetch_status()
    # GIPPO accepted a login, but process state still says the challenge is pending.
    server.authenticated = True
    forwarder.create_status = forwarder.poll_status = 503
    with client(server, path, forwarder) as restarted:
        restarted.fetch_status()
    assert server.code_requests == 0
    assert ("GET", "/internal/v1/challenges/challenge-1") not in forwarder.calls


def test_expired_session_starts_new_challenge_after_cooldown(
    tmp_path: Path, clock: list[float]
) -> None:
    server = CabinetServer()
    path = tmp_path / "session.json"
    with client(server, path) as instance:
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        instance.submit_sms_code("12345")
        server.authenticated = False
        with pytest.raises(GippoLoginThrottled):
            instance.fetch_status()
        clock[0] += 900
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
    assert server.sms_requests == 2


def test_only_three_distinct_codes_and_no_duplicate_auto_submission(
    tmp_path: Path, clock: list[float]
) -> None:
    server = CabinetServer()
    server.step2_payload = {"err": ["PROP[SMS_CODE]"]}
    forwarder = ForwarderServer(clock)
    with client(server, tmp_path / "session.json", forwarder) as instance:
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        forwarder.status = "received"
        with pytest.raises(GippoAuthenticationError):
            instance.fetch_status()
        clock[0] += 5
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        assert server.code_requests == 1
        for code in ("56789", "90123"):
            with pytest.raises(GippoAuthenticationError):
                instance.submit_sms_code(code)
        with pytest.raises(GippoLoginThrottled):
            instance.submit_sms_code("34567")
    assert server.sms_requests == 1
    assert server.code_requests == 3


def test_invalid_code_never_sends_sms(tmp_path: Path) -> None:
    server = CabinetServer()
    with client(server, tmp_path / "session.json") as instance:
        for code in ("1234", "123456", "١٢٣٤٥"):
            with pytest.raises(GippoAuthenticationError):
                instance.submit_sms_code(code)
        with pytest.raises(GippoAuthenticationError, match="no pending"):
            instance.submit_sms_code("12345")
    assert server.sms_requests == server.code_requests == 0


def test_concurrent_users_create_one_sms_challenge(tmp_path: Path, clock: list[float]) -> None:
    server = CabinetServer()
    with client(server, tmp_path / "session.json") as instance:

        def fetch() -> None:
            with pytest.raises(GippoSmsRequired):
                instance.fetch_status()

        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda _: fetch(), range(6)))
    assert server.sms_requests == 1


def test_separate_clients_reload_pending_state_before_sending(
    tmp_path: Path, clock: list[float]
) -> None:
    server = CabinetServer()
    path = tmp_path / "session.json"
    with client(server, path) as first, client(server, path) as second:
        with pytest.raises(GippoSmsRequired):
            first.fetch_status()
        with pytest.raises(GippoSmsRequired):
            second.fetch_status()
    assert server.sms_requests == 1


def test_corrupt_or_foreign_session_fails_closed(tmp_path: Path, clock: list[float]) -> None:
    server = CabinetServer()
    path = tmp_path / "session.json"
    path.write_text("broken", encoding="utf-8")
    path.chmod(0o600)
    with client(server, path) as instance, pytest.raises(GippoError, match="protected"):
        instance.fetch_status()
    path.unlink()
    with client(server, path) as first, pytest.raises(GippoSmsRequired):
        first.fetch_status()
    with (
        GippoClient(
            "+375291234568", session_path=path, transport=httpx.MockTransport(server)
        ) as other,
        pytest.raises(GippoError, match="protected"),
    ):
        other.fetch_status()
    assert server.sms_requests == 1


def test_forwarder_poll_failure_preserves_manual_confirmation(
    tmp_path: Path, clock: list[float]
) -> None:
    server = CabinetServer()
    forwarder = ForwarderServer(clock)
    with client(server, tmp_path / "session.json", forwarder) as instance:
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        forwarder.poll_status = 503
        with pytest.raises(GippoSmsRequired):
            instance.fetch_status()
        instance.submit_sms_code("12345")
    assert server.sms_requests == server.code_requests == 1
