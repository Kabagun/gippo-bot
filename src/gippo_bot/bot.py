"""Telegram application entry point."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from .cabinet import (
    GippoAuthenticationError,
    GippoClient,
    GippoError,
    GippoLoginThrottled,
    GippoSmsRequired,
)
from .config import BotSettings, SettingsError
from .formatting import format_status

LOGGER = logging.getLogger(__name__)
REFRESH_CALLBACK = "refresh_status:v3"
LEGACY_CALLBACKS = (
    "refresh_status",
    "refresh_status:v2",
    "restart_start:v3",
    "show_card:gippo:v2",
    "show_card:belmarket:v2",
)
LEGACY_CALLBACK_PATTERN = "^(?:" + "|".join(re.escape(value) for value in LEGACY_CALLBACKS) + ")$"
LEGACY_CALLBACK_MESSAGE = "Это меню устарело. Вызовите /start."


def _refresh_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Обновить", callback_data=REFRESH_CALLBACK)]]
    )


def _is_authorized(update: Update, settings: BotSettings) -> bool:
    user = update.effective_user
    return user is not None and (settings.open_access or user.id in settings.allowed_user_ids)


async def _close_cabinet(application: Application) -> None:
    """Close the shared cabinet session after Telegram shuts down."""

    client: GippoClient | None = application.bot_data.get("gippo_client")
    if client is not None:
        client.close()


async def _configure_bot_commands(application: Application) -> None:
    """Expose only `/start` in Telegram's command menu."""

    await application.bot.set_my_commands(
        [BotCommand(command="start", description="Показать скидку и обе карты")]
    )


async def _deny(update: Update) -> None:
    if update.callback_query is not None:
        await update.callback_query.answer("Нет доступа", show_alert=True)
    elif update.effective_message is not None:
        await update.effective_message.reply_text("Нет доступа.")


async def _load_message(client: GippoClient, *, owner: bool = False) -> str:
    try:
        status = await asyncio.to_thread(client.fetch_status)
    except GippoSmsRequired as exc:
        if owner:
            waiting = "Ожидаю SMS для входа в GIPPO. "
            if exc.automatic:
                waiting += "Код будет получен автоматически. "
            return (
                waiting + "При необходимости отправьте /gippo_sms и четырёхзначный код в этом чате."
            )
        return "Восстанавливается доступ к GIPPO. Обновите данные через несколько минут."
    except GippoLoginThrottled:
        return "Вход в GIPPO временно отложен. Попробуйте обновить данные позже."
    except GippoError:
        LOGGER.exception("Could not load the GIPPO cabinet status")
        return "Не удалось получить данные GIPPO. Попробуйте ещё раз позже."
    except Exception:
        LOGGER.exception("Unexpected error while loading the GIPPO cabinet status")
        return "Временная ошибка. Попробуйте ещё раз позже."
    return format_status(status, telegram_html=True)


def _validate_card_images(settings: BotSettings) -> None:
    invalid: list[str] = []
    for name, path in (
        ("GIPPO_CARD_IMAGE_PATH", settings.gippo_card_image_path),
        ("BELMARKET_CARD_IMAGE_PATH", settings.belmarket_card_image_path),
    ):
        try:
            with path.open("rb") as image:
                is_png = image.read(8) == b"\x89PNG\r\n\x1a\n"
        except OSError:
            is_png = False
        if not is_png:
            invalid.append(name)
    if invalid:
        raise SettingsError(f"Card image must be a readable PNG file: {', '.join(invalid)}")


async def _send_card(
    update: Update,
    *,
    image_path: Path,
    caption: str,
) -> None:
    message = update.effective_message
    if message is None:
        return

    try:
        with image_path.open("rb") as image:
            await message.reply_photo(photo=image, caption=caption)
    except (OSError, TelegramError):
        LOGGER.exception("Could not send loyalty card image")
        await message.reply_text("Не удалось отправить карту. Попробуйте ещё раз позже.")


async def _send_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    client: GippoClient = context.application.bot_data["gippo_client"]
    settings: BotSettings | None = context.application.bot_data.get("settings")
    message = await _load_message(client, owner=_is_owner(update, settings))
    if update.effective_message is not None:
        await update.effective_message.reply_text(
            message,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )


async def _send_start_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: BotSettings = context.application.bot_data["settings"]
    await _send_card(
        update,
        image_path=settings.gippo_card_image_path,
        caption="Карта ГИППО «АсобаЯ»",
    )
    await _send_card(
        update,
        image_path=settings.belmarket_card_image_path,
        caption="Карта Белмаркет «Хамелеон»",
    )
    await _send_status(update, context, reply_markup=_refresh_keyboard())


async def show_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/start` by sending both loyalty cards followed by status."""

    settings: BotSettings = context.application.bot_data["settings"]
    if not _is_authorized(update, settings):
        await _deny(update)
        return

    await _send_start_messages(update, context)


async def refresh_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Update only the existing status message after its button is pressed."""

    settings: BotSettings = context.application.bot_data["settings"]
    if not _is_authorized(update, settings):
        await _deny(update)
        return

    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer("Обновляю…")
    except TelegramError:
        LOGGER.warning("Could not acknowledge the refresh callback", exc_info=True)
    client: GippoClient = context.application.bot_data["gippo_client"]
    message = await _load_message(client, owner=_is_owner(update, settings))
    try:
        await query.edit_message_text(
            message,
            parse_mode=ParseMode.HTML,
            reply_markup=_refresh_keyboard(),
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def retire_legacy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Retire every command button from earlier bot menus."""

    settings: BotSettings = context.application.bot_data["settings"]
    if not _is_authorized(update, settings):
        await _deny(update)
        return

    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer(LEGACY_CALLBACK_MESSAGE, show_alert=True)
    except TelegramError:
        LOGGER.warning("Could not acknowledge a legacy callback", exc_info=True)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        LOGGER.warning("Could not remove a legacy command button", exc_info=True)


def _is_owner(update: Update, settings: BotSettings | None) -> bool:
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    return bool(
        settings
        and settings.owner_user_id
        and user
        and user.id == settings.owner_user_id
        and chat
        and chat.type == "private"
    )


async def confirm_sms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept a manual GIPPO code only from the configured owner in private."""

    settings: BotSettings = context.application.bot_data["settings"]
    if not _is_owner(update, settings):
        await _deny(update)
        return
    message = update.effective_message
    if message is None:
        return
    if len(context.args) != 1 or not re.fullmatch(r"[0-9]{4}", context.args[0]):
        await message.reply_text("Отправьте /gippo_sms и четырёхзначный код из SMS.")
        return
    # Do not leave the short-lived credential visible in the chat history.
    with suppress(TelegramError):
        await message.delete()
    client: GippoClient = context.application.bot_data["gippo_client"]
    try:
        status = await asyncio.to_thread(client.submit_sms_code, context.args[0])
    except GippoLoginThrottled:
        text = "Код больше не принимается. Следующая попытка входа будет доступна позже."
    except GippoAuthenticationError:
        text = "Код не принят или вход уже завершён. Проверьте код и обновите данные."
    except GippoError:
        text = "Не удалось подтвердить вход в GIPPO. Попробуйте обновить данные позже."
    else:
        text = "Вход в GIPPO восстановлен.\n\n" + format_status(status, telegram_html=True)
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_refresh_keyboard())


def build_application(settings: BotSettings) -> Application:
    """Build a configured Telegram application without starting polling."""

    _validate_card_images(settings)
    cabinet = settings.cabinet
    client = GippoClient(
        cabinet.login,
        cabinet.password,
        **cabinet.client_options(),
    )
    application = (
        ApplicationBuilder()
        .token(settings.token)
        .post_init(_configure_bot_commands)
        .post_shutdown(_close_cabinet)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["gippo_client"] = client
    application.add_handler(CommandHandler("start", show_start))
    application.add_handler(CommandHandler("gippo_sms", confirm_sms))
    application.add_handler(CallbackQueryHandler(refresh_status, pattern=f"^{REFRESH_CALLBACK}$"))
    application.add_handler(
        CallbackQueryHandler(retire_legacy_callback, pattern=LEGACY_CALLBACK_PATTERN)
    )
    return application


def main() -> None:
    """Load configuration and run the bot with Telegram long polling."""

    try:
        settings = BotSettings.from_environment()
    except SettingsError as exc:
        raise SystemExit(str(exc)) from exc

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=getattr(logging, settings.log_level, logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        application = build_application(settings)
    except SettingsError as exc:
        raise SystemExit(str(exc)) from exc
    application.run_polling(allowed_updates=Update.ALL_TYPES)
