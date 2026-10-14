"""Automatic completion updates waiting messages without requesting more SMS."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import Chat, Message
from telegram.error import BadRequest, TelegramError
from test_bot import _settings
from test_sms_login import CabinetServer, ForwarderServer, client

from gippo_bot.bot import (
    _LoginRecovery,
    _send_status,
    _stop_recovery,
    refresh_status,
)
from gippo_bot.cabinet import GippoLoginThrottled, GippoSmsRequired, parse_personal_page

STATUS = parse_personal_page(
    "Ваша текущая скидка 1% Ваши покупки в этом месяце 247.27 "
    "До следующего уровня осталось 252.73"
)


def waiting_message(chat_id=1, message_id=1):
    return SimpleNamespace(chat_id=chat_id, message_id=message_id, edit_text=AsyncMock())


@pytest.fixture
def immediate_poll(monkeypatch):
    original_sleep = asyncio.sleep
    delays = []

    async def sleep(delay):
        delays.append(delay)
        await original_sleep(0)

    monkeypatch.setattr("gippo_bot.bot.asyncio.sleep", sleep)
    return delays


def test_one_monitor_updates_all_waiters_and_deduplicates_messages(immediate_poll):
    async def run():
        pending = GippoSmsRequired(automatic=True, expires_at=time.time() + 300)
        client = Mock()
        client.poll_sms_login.side_effect = [pending, STATUS]
        recovery = _LoginRecovery(client)
        first, second = waiting_message(), waiting_message(chat_id=2)
        recovery.watch(first, pending)
        task = recovery._task
        recovery.watch(first, pending)
        recovery.watch(second, pending)
        assert recovery._task is task
        await task
        for message in (first, second):
            message.edit_text.assert_awaited_once()
            assert "247,27" in message.edit_text.await_args.args[0]
        assert recovery._messages == {}
        assert client.poll_sms_login.call_count == 2
        client.fetch_status.assert_not_called()
        await recovery.close()

    asyncio.run(run())
    assert immediate_poll == [5, 5]


def test_expiry_updates_message_once_and_stops(immediate_poll):
    async def run():
        pending = GippoSmsRequired(automatic=True, expires_at=time.time() + 300)
        client = Mock()
        client.poll_sms_login.side_effect = GippoLoginThrottled(time.time() + 900)
        recovery = _LoginRecovery(client)
        message = waiting_message()
        recovery.watch(message, pending)
        await recovery._task
        assert "отложен" in message.edit_text.await_args.args[0]
        assert client.poll_sms_login.call_count == 1
        client.fetch_status.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("error", [BadRequest("Message is not modified"), TelegramError("deleted")])
def test_one_uneditable_message_does_not_block_other_waiters(immediate_poll, error):
    async def run():
        client = Mock()
        client.poll_sms_login.return_value = STATUS
        recovery = _LoginRecovery(client)
        first, second = waiting_message(), waiting_message(chat_id=2)
        second.edit_text.side_effect = error
        pending = GippoSmsRequired(automatic=True, expires_at=time.time() + 300)
        recovery.watch(first, pending)
        recovery.watch(second, pending)
        await recovery._task
        first.edit_text.assert_awaited_once()

    asyncio.run(run())


def test_manual_mode_does_not_start_monitor():
    async def run():
        client = Mock()
        recovery = _LoginRecovery(client)
        recovery.watch(waiting_message(), GippoSmsRequired(automatic=False, expires_at=100))
        assert recovery._task is None
        assert recovery._messages == {}

    asyncio.run(run())


def test_shutdown_cancels_wait_and_preserves_pending_login():
    async def run():
        client = Mock()
        recovery = _LoginRecovery(client)
        message = waiting_message()
        recovery.watch(message, GippoSmsRequired(automatic=True, expires_at=time.time() + 300))
        application = SimpleNamespace(bot_data={"login_recovery": recovery})
        await _stop_recovery(application)
        assert recovery._task.cancelled()
        assert recovery._messages == {}
        client.poll_sms_login.assert_not_called()
        client.close.assert_not_called()
        message.edit_text.assert_not_awaited()

    asyncio.run(run())


def test_start_status_is_automatically_replaced_after_code_arrives(immediate_poll):
    async def run():
        pending = GippoSmsRequired(automatic=True, expires_at=time.time() + 300)
        client = Mock()
        client.fetch_status.side_effect = pending
        client.poll_sms_login.return_value = STATUS
        message = waiting_message()
        request = SimpleNamespace(reply_text=AsyncMock(return_value=message))
        update = SimpleNamespace(effective_message=request)
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"gippo_client": client}))
        await _send_status(update, context)
        await context.application.bot_data["login_recovery"]._task
        assert "автоматически" in request.reply_text.await_args.args[0]
        request.reply_text.assert_awaited_once()
        message.edit_text.assert_awaited_once()
        client.fetch_status.assert_called_once()

    asyncio.run(run())


def test_refresh_of_same_pending_text_still_starts_monitor(immediate_poll):
    async def run():
        pending = GippoSmsRequired(automatic=True, expires_at=time.time() + 300)
        client = Mock()
        client.fetch_status.side_effect = pending
        client.poll_sms_login.return_value = STATUS
        bot = SimpleNamespace(edit_message_text=AsyncMock())
        message = Message(message_id=7, date=None, chat=Chat(id=1, type="private"))
        message.set_bot(bot)
        query = SimpleNamespace(
            answer=AsyncMock(), message=message,
            edit_message_text=AsyncMock(side_effect=BadRequest("Message is not modified")),
        )
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), callback_query=query)
        settings = _settings(open_access=True, allowed_user_ids=frozenset())
        application = SimpleNamespace(bot_data={"settings": settings, "gippo_client": client})
        await refresh_status(update, SimpleNamespace(application=application))
        await application.bot_data["login_recovery"]._task
        bot.edit_message_text.assert_awaited_once()
        assert "247,27" in bot.edit_message_text.await_args.kwargs["text"]

    asyncio.run(run())


def test_status_request_and_forwarded_code_complete_the_whole_login(
    tmp_path, monkeypatch, immediate_poll
):
    async def run():
        now = [time.time()]
        monkeypatch.setattr("gippo_bot.cabinet.time.time", lambda: now[0])
        server = CabinetServer()
        forwarder = ForwarderServer(now)
        forwarder.status = "received"
        message = waiting_message()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=AsyncMock(return_value=message))
        )
        with client(server, tmp_path / "session.json", forwarder) as instance:
            application = SimpleNamespace(bot_data={"gippo_client": instance})
            await _send_status(update, SimpleNamespace(application=application))
            assert server.sms_requests == 1
            await application.bot_data["login_recovery"]._task
            assert server.authenticated
            assert server.code_requests == server.sms_requests == 1
            assert forwarder.calls[-1] == ("DELETE", "/internal/v1/challenges/challenge-1")
            assert "247,27" in message.edit_text.await_args.args[0]

    asyncio.run(run())
