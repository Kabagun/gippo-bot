from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Event, Lock

import httpx
import pytest

from gippo_bot.cabinet import (
    GippoAuthenticationError,
    GippoClient,
    GippoError,
    GippoPageError,
    parse_personal_page,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_personal_page() -> None:
    status = parse_personal_page((FIXTURES / "personal.html").read_text(encoding="utf-8"))

    assert status.discount_percent == Decimal("1")
    assert status.monthly_purchases == Decimal("247.27")
    assert status.until_next_level == Decimal("252.73")


def test_parse_personal_page_accepts_grouped_values() -> None:
    html = """
    Ваша текущая скидка 2,5%
    Ваши покупки в этом месяце 1&nbsp;247,27
    До следующего уровня осталось 2 252.73
    """

    status = parse_personal_page(html)

    assert status.discount_percent == Decimal("2.5")
    assert status.monthly_purchases == Decimal("1247.27")
    assert status.until_next_level == Decimal("2252.73")


def test_parse_personal_page_accepts_missing_next_level_at_highest_tier() -> None:
    html = """
    <section class="discount-program">
      Ваша текущая скидка5%
      Ваши покупки в этом месяце 3,824.71
    </section>
    """

    status = parse_personal_page(html)

    assert status.discount_percent == Decimal("5")
    assert status.monthly_purchases == Decimal("3824.71")
    assert status.until_next_level == Decimal("0")


def test_parse_personal_page_requires_next_level_below_highest_tier() -> None:
    html = """
    Ваша текущая скидка 3%
    Ваши покупки в этом месяце 750.50
    """

    with pytest.raises(GippoPageError, match="until_next_level"):
        parse_personal_page(html)


@pytest.mark.parametrize(
    ("raw_purchases", "expected_purchases"),
    (
        ("1,022", Decimal("1022")),
        ("1,022.45", Decimal("1022.45")),
        ("1.022", Decimal("1022")),
        ("1.022,45", Decimal("1022.45")),
        ("1\u202f022,45", Decimal("1022.45")),
    ),
)
def test_parse_personal_page_accepts_punctuated_thousands(
    raw_purchases: str, expected_purchases: Decimal
) -> None:
    html = f"""
    Ваша текущая скидка 1%
    Ваши покупки в этом месяце {raw_purchases}
    До следующего уровня осталось 0.00
    """

    status = parse_personal_page(html)

    assert status.monthly_purchases == expected_purchases
    assert status.until_next_level == Decimal("0.00")


@pytest.mark.parametrize(
    "raw_purchases",
    (
        "1,2.34",
        "1.23,4",
        "12 34,56",
        "1,234.",
        "1,234.56,",
        "1.234,56.",
    ),
)
def test_parse_personal_page_rejects_malformed_grouping(raw_purchases: str) -> None:
    html = f"""
    Ваша текущая скидка 1%
    Ваши покупки в этом месяце {raw_purchases}
    До следующего уровня осталось 0.00
    """

    with pytest.raises(GippoPageError, match="invalid numeric value"):
        parse_personal_page(html)


def test_parse_personal_page_rejects_missing_value() -> None:
    with pytest.raises(GippoPageError, match="monthly_purchases"):
        parse_personal_page("Ваша текущая скидка 1%")


def test_client_posts_multipart_and_reuses_session_cookie() -> None:
    page = (FIXTURES / "personal.html").read_text(encoding="utf-8")
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/local/ajax/auth.php":
            assert request.method == "POST"
            assert request.headers["x-requested-with"] == "XMLHttpRequest"
            assert "multipart/form-data" in request.headers["content-type"]
            return httpx.Response(200, headers={"set-cookie": "PHPSESSID=valid; Path=/"})
        assert request.url.path == "/personal/"
        assert "PHPSESSID=valid" in request.headers["cookie"]
        return httpx.Response(200, text=page)

    with GippoClient(
        "+375000000000",
        "secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        status = client.fetch_status()
        second_status = client.fetch_status()

    assert status.monthly_purchases == Decimal("247.27")
    assert second_status.monthly_purchases == Decimal("247.27")
    assert requests == [
        ("POST", "/local/ajax/auth.php"),
        ("GET", "/personal/"),
        ("GET", "/personal/"),
    ]


def test_client_reauthenticates_once_when_session_expires() -> None:
    page = (FIXTURES / "personal.html").read_text(encoding="utf-8")
    auth_count = 0
    personal_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_count, personal_count
        if request.url.path == "/local/ajax/auth.php":
            auth_count += 1
            session = f"session{auth_count}"
            return httpx.Response(200, headers={"set-cookie": f"PHPSESSID={session}; Path=/"})

        personal_count += 1
        if personal_count == 2:
            return httpx.Response(
                200,
                text='<input name="PROP_RQ[PASSWORD]">',
                request=httpx.Request("GET", "https://cabinet.gippo.by/index.php"),
            )
        expected_session = "session1" if personal_count == 1 else "session2"
        assert f"PHPSESSID={expected_session}" in request.headers["cookie"]
        return httpx.Response(200, text=page)

    with GippoClient(
        "login",
        "password",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.fetch_status()
        status = client.fetch_status()

    assert status.monthly_purchases == Decimal("247.27")
    assert auth_count == 2
    assert personal_count == 3


def test_client_rejects_http_auth_failure() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(401)

    with (
        GippoClient(
            "login",
            "wrong",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(GippoAuthenticationError),
    ):
        client.fetch_status()

    assert requests == ["/local/ajax/auth.php"]


def test_client_rejects_redirected_login_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/local/ajax/auth.php":
            return httpx.Response(200)
        return httpx.Response(
            200,
            text='<input name="PROP_RQ[PASSWORD]">',
            request=httpx.Request("GET", "https://cabinet.gippo.by/index.php"),
        )

    with (
        GippoClient("login", "wrong", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(GippoAuthenticationError),
    ):
        client.fetch_status()


def test_client_wraps_network_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    with (
        GippoClient("login", "password", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(GippoError, match="communicate"),
    ):
        client.fetch_status()


def test_client_wraps_personal_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/local/ajax/auth.php":
            return httpx.Response(200, headers={"set-cookie": "PHPSESSID=valid; Path=/"})
        return httpx.Response(500, request=request)

    with (
        GippoClient("login", "password", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(GippoError, match="communicate"),
    ):
        client.fetch_status()


def test_client_serializes_concurrent_fetches_and_logs_in_once() -> None:
    page = (FIXTURES / "personal.html").read_text(encoding="utf-8")
    auth_started = Event()
    release_auth = Event()
    count_lock = Lock()
    auth_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_count
        if request.url.path == "/local/ajax/auth.php":
            with count_lock:
                auth_count += 1
            auth_started.set()
            assert release_auth.wait(timeout=5)
            return httpx.Response(200, headers={"set-cookie": "PHPSESSID=valid; Path=/"})
        assert "PHPSESSID=valid" in request.headers["cookie"]
        return httpx.Response(200, text=page)

    with (
        GippoClient(
            "login",
            "password",
            transport=httpx.MockTransport(handler),
        ) as client,
        ThreadPoolExecutor(max_workers=4) as executor,
    ):
        futures = [executor.submit(client.fetch_status) for _ in range(4)]
        assert auth_started.wait(timeout=5)
        release_auth.set()
        statuses = [future.result(timeout=5) for future in futures]

    assert auth_count == 1
    assert all(status.monthly_purchases == Decimal("247.27") for status in statuses)


def test_client_close_is_idempotent_and_prevents_future_requests() -> None:
    client = GippoClient(
        "login",
        "password",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
    )

    client.close()
    client.close()

    with pytest.raises(GippoError, match="closed"):
        client.fetch_status()
