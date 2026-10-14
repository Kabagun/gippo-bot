import httpx
import pytest

from gippo_bot.sms import SmsForwarderClient, SmsServiceError


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"id": "one", "status": "received", "code": "123456"},
        {"id": "one", "status": "received", "code": "١٢٣٤٥"},
        {"id": "one", "status": "received", "code": "1234"},
        {"id": "one", "status": "waiting", "code": "1234"},
        {"id": "other", "status": "waiting", "code": None},
    ],
)
def test_poll_rejects_invalid_or_wrong_challenge(payload: object) -> None:
    instance = SmsForwarderClient(
        "http://127.0.0.1:18090",
        "token",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    try:
        with pytest.raises(SmsServiceError):
            instance.poll("one")
    finally:
        instance.close()


def test_poll_accepts_five_ascii_digit_code() -> None:
    instance = SmsForwarderClient(
        "http://127.0.0.1:18090",
        "token",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"id": "one", "status": "received", "code": "12345"},
            )
        ),
    )
    try:
        challenge = instance.poll("one")
    finally:
        instance.close()
    assert challenge.id == "one"
    assert challenge.status == "received"
    assert challenge.code == "12345"


def test_sender_override_is_used_without_following_redirects() -> None:
    import json

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert json.loads(request.content)["sender"] == "GippoSender"
        return httpx.Response(307, headers={"location": "http://elsewhere.example/"})

    instance = SmsForwarderClient(
        "http://127.0.0.1:18090",
        "token",
        sender="GippoSender",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(SmsServiceError):
            instance.create(300)
    finally:
        instance.close()
    assert len(calls) == 1
