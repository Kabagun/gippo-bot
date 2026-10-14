import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.constants import ParseMode
from telegram.error import BadRequest

from gippo_bot.bot import (
    LEGACY_CALLBACK_MESSAGE,
    REFRESH_CALLBACK,
    _configure_bot_commands,
    _is_authorized,
    _is_owner,
    _load_message,
    _refresh_keyboard,
    _send_card,
    _send_start_messages,
    _send_status,
    _StatusMessage,
    _validate_card_images,
    build_application,
    confirm_sms,
    refresh_status,
    retire_legacy_callback,
    show_start,
)
from gippo_bot.cabinet import DEFAULT_BASE_URL, GippoLoginThrottled, GippoSmsRequired
from gippo_bot.config import BotSettings, CabinetSettings, SettingsError


def _settings(
    *,
    open_access: bool,
    allowed_user_ids: frozenset[int],
    gippo_card_image_path: Path = Path("gippo.png"),
    belmarket_card_image_path: Path = Path("belmarket.png"),
) -> BotSettings:
    return BotSettings(
        token="token",
        open_access=open_access,
        allowed_user_ids=allowed_user_ids,
        cabinet=CabinetSettings(
            login="login",
            password="password",
            auth_mode="password",
            base_url=DEFAULT_BASE_URL,
        ),
        gippo_card_image_path=gippo_card_image_path,
        belmarket_card_image_path=belmarket_card_image_path,
    )


def test_open_access_accepts_any_user() -> None:
    update = SimpleNamespace(effective_user=SimpleNamespace(id=999))

    assert _is_authorized(update, _settings(open_access=True, allowed_user_ids=frozenset()))


def test_restricted_access_uses_allow_list() -> None:
    allowed = _settings(open_access=False, allowed_user_ids=frozenset({123}))

    assert _is_authorized(SimpleNamespace(effective_user=SimpleNamespace(id=123)), allowed)
    assert not _is_authorized(SimpleNamespace(effective_user=SimpleNamespace(id=999)), allowed)


def test_latest_message_keyboard_has_only_refresh() -> None:
    keyboard = _refresh_keyboard().inline_keyboard

    assert [button.callback_data for row in keyboard for button in row] == [
        REFRESH_CALLBACK,
    ]


def test_card_images_are_required_before_application_start(tmp_path: Path) -> None:
    gippo_card = tmp_path / "gippo.png"
    belmarket_card = tmp_path / "belmarket.png"
    settings = _settings(
        open_access=True,
        allowed_user_ids=frozenset(),
        gippo_card_image_path=gippo_card,
        belmarket_card_image_path=belmarket_card,
    )

    with pytest.raises(SettingsError, match="GIPPO_CARD_IMAGE_PATH"):
        _validate_card_images(settings)

    png_signature = b"\x89PNG\r\n\x1a\n"
    gippo_card.write_bytes(png_signature + b"gippo")
    belmarket_card.write_bytes(png_signature + b"belmarket")

    _validate_card_images(settings)


def test_legacy_callback_is_removed_and_asks_for_start() -> None:
    settings = _settings(open_access=True, allowed_user_ids=frozenset())
    query = SimpleNamespace(answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        callback_query=query,
    )
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"settings": settings}))

    asyncio.run(retire_legacy_callback(update, context))

    query.answer.assert_awaited_once_with(LEGACY_CALLBACK_MESSAGE, show_alert=True)
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


def test_refresh_updates_only_existing_status_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(open_access=True, allowed_user_ids=frozenset())
    query = SimpleNamespace(
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        callback_query=query,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"settings": settings, "gippo_client": object()})
    )
    monkeypatch.setattr(
        "gippo_bot.bot._load_message", AsyncMock(return_value=_StatusMessage("new status"))
    )
    send_card = AsyncMock()
    monkeypatch.setattr("gippo_bot.bot._send_card", send_card)

    asyncio.run(refresh_status(update, context))

    query.answer.assert_awaited_once_with("Обновляю…")
    query.edit_message_text.assert_awaited_once_with(
        "new status",
        parse_mode=ParseMode.HTML,
        reply_markup=_refresh_keyboard(),
    )
    send_card.assert_not_awaited()


def test_status_message_uses_html_formatting(monkeypatch: pytest.MonkeyPatch) -> None:
    effective_message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=effective_message)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"gippo_client": object()}))
    monkeypatch.setattr(
        "gippo_bot.bot._load_message",
        AsyncMock(return_value=_StatusMessage("<b>status</b>")),
    )

    asyncio.run(_send_status(update, context, reply_markup=_refresh_keyboard()))

    effective_message.reply_text.assert_awaited_once_with(
        "<b>status</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=_refresh_keyboard(),
    )


def test_unchanged_refresh_status_does_not_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(open_access=True, allowed_user_ids=frozenset())
    query = SimpleNamespace(
        answer=AsyncMock(),
        edit_message_text=AsyncMock(side_effect=BadRequest("Message is not modified")),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        callback_query=query,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"settings": settings, "gippo_client": object()})
    )
    monkeypatch.setattr(
        "gippo_bot.bot._load_message", AsyncMock(return_value=_StatusMessage("same status"))
    )

    asyncio.run(refresh_status(update, context))

    query.edit_message_text.assert_awaited_once()


def test_start_sends_complete_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(open_access=True, allowed_user_ids=frozenset())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=999))
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"settings": settings}))
    send_complete = AsyncMock()
    monkeypatch.setattr("gippo_bot.bot._send_start_messages", send_complete)

    asyncio.run(show_start(update, context))

    send_complete.assert_awaited_once_with(update, context)


def test_start_messages_order_cards_then_status_with_refresh_only_on_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(open_access=True, allowed_user_ids=frozenset())
    update = SimpleNamespace()
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"settings": settings}))
    events: list[str] = []
    status_markup: list[object] = []

    async def record_status(*_args: object, reply_markup: object | None = None) -> None:
        events.append("status")
        status_markup.append(reply_markup)

    async def record_card(*_args: object, image_path: Path, caption: str) -> None:
        del image_path
        events.append(caption)

    send_card = AsyncMock(side_effect=record_card)
    monkeypatch.setattr("gippo_bot.bot._send_status", record_status)
    monkeypatch.setattr("gippo_bot.bot._send_card", send_card)

    asyncio.run(_send_start_messages(update, context))

    assert events == ["Карта ГИППО «АсобаЯ»", "Карта Белмаркет «Хамелеон»", "status"]
    assert status_markup == [_refresh_keyboard()]
    assert send_card.await_count == 2
    gippo_call, belmarket_call = send_card.await_args_list
    assert gippo_call.kwargs == {
        "image_path": settings.gippo_card_image_path,
        "caption": "Карта ГИППО «АсобаЯ»",
    }
    assert belmarket_call.kwargs["image_path"] == settings.belmarket_card_image_path
    assert belmarket_call.kwargs["caption"] == "Карта Белмаркет «Хамелеон»"


def test_send_card_preserves_original_bytes_without_buttons(tmp_path: Path) -> None:
    card = tmp_path / "card.png"
    card.write_bytes(b"original-image")
    captured: dict[str, object] = {}

    async def capture_photo(*, photo: object, caption: str) -> None:
        captured.update(image=photo.read(), caption=caption)

    message = SimpleNamespace(reply_photo=AsyncMock(side_effect=capture_photo))
    update = SimpleNamespace(effective_message=message)
    asyncio.run(_send_card(update, image_path=card, caption="card"))

    assert captured == {
        "image": b"original-image",
        "caption": "card",
    }


def test_command_menu_contains_only_start() -> None:
    set_commands = AsyncMock()
    application = SimpleNamespace(bot=SimpleNamespace(set_my_commands=set_commands))

    asyncio.run(_configure_bot_commands(application))

    commands = set_commands.await_args.args[0]
    assert [(command.command, command.description) for command in commands] == [
        ("start", "Показать скидку и обе карты")
    ]


def test_application_registers_only_start_and_refresh_actions(tmp_path: Path) -> None:
    png_signature = b"\x89PNG\r\n\x1a\n"
    gippo_card = tmp_path / "gippo.png"
    belmarket_card = tmp_path / "belmarket.png"
    gippo_card.write_bytes(png_signature)
    belmarket_card.write_bytes(png_signature)
    settings = _settings(
        open_access=True,
        allowed_user_ids=frozenset(),
        gippo_card_image_path=gippo_card,
        belmarket_card_image_path=belmarket_card,
    )

    application = build_application(settings)
    try:
        assert [handler.callback.__name__ for handler in application.handlers[0]] == [
            "show_start",
            "confirm_sms",
            "refresh_status",
            "retire_legacy_callback",
        ]
    finally:
        application.bot_data["gippo_client"].close()


@pytest.mark.parametrize(
    ("user_id", "chat_type", "accepted"),
    [(123, "private", True), (999, "private", False), (123, "group", False)],
)
def test_manual_confirmation_is_private_owner_only(user_id, chat_type, accepted) -> None:
    settings = replace(_settings(open_access=True, allowed_user_ids=frozenset()), owner_user_id=123)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(type=chat_type)
    )
    assert _is_owner(update, settings) is accepted


def test_unauthorized_user_cannot_submit_or_start_sms() -> None:
    settings = replace(_settings(open_access=True, allowed_user_ids=frozenset()), owner_user_id=123)
    client = Mock()
    message = SimpleNamespace(reply_text=AsyncMock(), delete=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_chat=SimpleNamespace(type="private"),
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"settings": settings, "gippo_client": client}),
        args=["12345"],
    )
    asyncio.run(confirm_sms(update, context))
    client.submit_sms_code.assert_not_called()
    client.fetch_status.assert_not_called()
    message.delete.assert_not_awaited()
    message.reply_text.assert_awaited_once_with("Нет доступа.")


def test_owner_can_finish_pending_sms_without_new_sms() -> None:
    from gippo_bot.cabinet import parse_personal_page

    settings = replace(_settings(open_access=True, allowed_user_ids=frozenset()), owner_user_id=123)
    client = Mock()
    client.submit_sms_code.return_value = parse_personal_page(
        "Ваша текущая скидка 1% Ваши покупки в этом месяце 247.27 "
        "До следующего уровня осталось 252.73"
    )
    message = SimpleNamespace(reply_text=AsyncMock(), delete=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(type="private"),
        effective_message=message,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"settings": settings, "gippo_client": client}),
        args=["12345"],
    )
    asyncio.run(confirm_sms(update, context))
    client.submit_sms_code.assert_called_once_with("12345")
    client.fetch_status.assert_not_called()
    message.delete.assert_awaited_once()
    assert "Вход в GIPPO восстановлен" in message.reply_text.await_args.args[0]


def test_public_pending_message_does_not_expose_owner_authentication_action() -> None:
    client = Mock()
    client.fetch_status.side_effect = GippoSmsRequired(automatic=True, expires_at=100)
    result = asyncio.run(_load_message(client))
    assert "/gippo_sms" not in result.text
    assert "автоматически" in result.text
    assert result.pending.automatic
    assert "/gippo_sms" in asyncio.run(_load_message(client, owner=True)).text
    client.fetch_status.side_effect = GippoLoginThrottled(100)
    assert "отложен" in asyncio.run(_load_message(client)).text
