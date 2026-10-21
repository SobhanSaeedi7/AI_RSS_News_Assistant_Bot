"""Application entry point shared by the bot and worker processes.

This module only wires up configuration and logging. The bot (Telegram
interaction) and worker (RSS polling, summarization, delivery) behaviors
are implemented here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from ai_rss_news_assistant.config import Settings
from ai_rss_news_assistant.db.base import Database
from ai_rss_news_assistant.db.repository import (
    create_feed,
    create_user,
    delete_feed,
    get_user_by_telegram_id,
    list_feeds,
)
from ai_rss_news_assistant.db.rss_worker import NewsWorker
from ai_rss_news_assistant.db.url_normalization import normalize_url
from ai_rss_news_assistant.logging import setup_logging

logger = logging.getLogger(__name__)

WELCOME_MESSAGE = (
    "سلام! به ربات خبری RSS خوش آمدید.\n\n"
    "دستورهای موجود:\n"
    "/start - ثبت نام و شروع کار با ربات\n"
    "/help - نمایش راهنما\n"
    "/addfeed <url> - اضافه کردن فید RSS جدید\n"
    "/listfeeds - مشاهده فیدهای شما\n"
    "/delfeed <id> - حذف یک فید"
)

HELP_MESSAGE = (
    "راهنما ربات خبری RSS\n\n"
    "دستورهای ممکن:\n"
    "/start - ثبت نام و شروع کار با ربات\n"
    "/help - نمایش راهنما\n"
    "/addfeed <url> - اضافه کردن فید RSS جدید\n"
    "/listfeeds - مشاهده فیدهای شما\n"
    "/delfeed <id> - حذف یک فید"
)

ADD_FEED_USAGE = (
    "لطفاً یک آدرس فید RSS ارائه دهید:\n"
    "/addfeed <url>"
)

ADD_FEED_INVALID = (
    "آدرس feed نامعتبر است. لطفاً یک URL معتبر (http/https) ارائه دهید."
)

ADD_FEED_SUCCESS = (
    "فید با موفقیت اضافه شد.\n"
    "آدرس: {url}\n"
    "عنوان: {title}"
)

ADD_FEED_DUPLICATE = (
    "این فید قبلاً برای شما اضافه شده است."
)

LIST_FEEDS_EMPTY = (
    "هیچ فیدی ثبت نشده است. از /addfeed برای اضافه کردن فید استفاده کنید."
)

LIST_FEEDS_HEADER = "فیدهای شما:\n\n"

LIST_FEED_ITEM = "{id}. {title} — {url} ({status})\n"

DEL_FEED_USAGE = (
    "لطفاً شناسه فید را ارائه دهید:\n"
    "/delfeed <id>"
)

DEL_FEED_SUCCESS = (
    "فید با شناسه {id} حذف شد."
)

DEL_FEED_NOT_FOUND = (
    "فیدی با شناسه {id} یافت نشد یا دسترسی به آن ندارید."
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start: find or create user, then welcome them."""
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    db: Database = context.application.bot_data["db"]
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if user is None:
            await create_user(session, telegram_user.id)

    await update.effective_message.reply_text(WELCOME_MESSAGE)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help: show available commands."""
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(HELP_MESSAGE)


async def add_feed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /addfeed <url>: add a new RSS feed for the current user."""
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    args = context.args
    if len(args) != 1:
        await update.effective_message.reply_text(ADD_FEED_USAGE)
        return

    url = args[0].strip()

    # Validate scheme is http/https before normalization
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        await update.effective_message.reply_text(ADD_FEED_INVALID)
        return

    try:
        normalized = normalize_url(url)
    except ValueError:
        await update.effective_message.reply_text(ADD_FEED_INVALID)
        return

    db: Database = context.application.bot_data["db"]
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if user is None:
            await create_user(session, telegram_user.id)

        # Check for duplicate by normalized URL
        existing_feeds = await list_feeds(session, user.id)
        for feed in existing_feeds:
            try:
                if normalize_url(feed.url) == normalized:
                    await update.effective_message.reply_text(ADD_FEED_DUPLICATE)
                    return
            except ValueError:
                continue

        try:
            feed = await create_feed(session, user.id, url)
        except IntegrityError as exc:
            await session.rollback()
            if _is_duplicate_feed_violation(exc):
                await update.effective_message.reply_text(ADD_FEED_DUPLICATE)
                return
            raise

    await update.effective_message.reply_text(
        ADD_FEED_SUCCESS.format(url=feed.url, title=feed.title or "(بدون عنوان)")
    )


async def list_feeds_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /listfeeds: list current user's feeds."""
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    db: Database = context.application.bot_data["db"]
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if user is None:
            await create_user(session, telegram_user.id)

        feeds = await list_feeds(session, user.id)

    if not feeds:
        await update.effective_message.reply_text(LIST_FEEDS_EMPTY)
        return

    lines = [LIST_FEEDS_HEADER]
    for feed in feeds:
        status = "فعال" if feed.active else "غیرفعال"
        title = feed.title or "(بدون عنوان)"
        lines.append(
            LIST_FEED_ITEM.format(id=feed.id, title=title, url=feed.url, status=status)
        )

    await update.effective_message.reply_text("".join(lines))


async def delete_feed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /delfeed <id>: delete a feed owned by the current user."""
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    args = context.args
    if len(args) != 1:
        await update.effective_message.reply_text(DEL_FEED_USAGE)
        return

    try:
        feed_id = uuid.UUID(args[0])
    except (ValueError, AttributeError):
        await update.effective_message.reply_text(DEL_FEED_NOT_FOUND.format(id=args[0]))
        return

    db: Database = context.application.bot_data["db"]
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if user is None:
            await create_user(session, telegram_user.id)

        deleted = await delete_feed(session, user.id, feed_id)

    if deleted:
        await update.effective_message.reply_text(DEL_FEED_SUCCESS.format(id=feed_id))
    else:
        await update.effective_message.reply_text(DEL_FEED_NOT_FOUND.format(id=feed_id))


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by Updates without leaking secrets."""
    logger.exception("Exception while handling an update: %s", context.error)


def _is_duplicate_feed_violation(exc: IntegrityError) -> bool:
    """Check whether an IntegrityError is a unique violation on (user_id, url).

    PostgreSQL/asyncpg reports unique violations with SQLSTATE 23505 and
    exposes the violated constraint name on the underlying exception.
    Other dialects can be matched by the constraint name in the message.
    """
    orig = getattr(exc, "orig", None)
    if orig is not None:
        pgcode = getattr(orig, "pgcode", None)
        if pgcode != "23505":
            return False
        constraint_name = getattr(orig, "constraint_name", None)
        if constraint_name is not None:
            return constraint_name == "uq_user_feeds_user_id_url"
        message = str(exc)
        return "uq_user_feeds_user_id_url" in message
    message = str(exc)
    return "uq_user_feeds_user_id_url" in message


def main() -> None:
    """Initialize the bot process and run Telegram polling."""
    settings = Settings()
    if settings.bot_token is None:
        raise ValueError("BOT_TOKEN is required for the bot service")
    setup_logging(settings.log_level)
    logger.info("bot process starting (app_env=%s)", settings.app_env)

    db = Database(settings.database_url)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = None
    try:
        application = (
            ApplicationBuilder()
            .token(settings.bot_token)
            .build()
        )
        application.bot_data["db"] = db

        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("addfeed", add_feed_command))
        application.add_handler(CommandHandler("listfeeds", list_feeds_command))
        application.add_handler(CommandHandler("delfeed", delete_feed_command))
        application.add_error_handler(_error_handler)

        try:
            loop.add_signal_handler(signal.SIGINT, loop.stop)
            loop.add_signal_handler(signal.SIGTERM, loop.stop)
        except (NotImplementedError, RuntimeError):
            # Signal handlers are not supported on this platform (e.g., Windows).
            # The worker will still run; it just can't be stopped via signals.
            logger.warning("Signal handlers not supported on this platform")

        loop.run_until_complete(application.initialize())
        loop.run_until_complete(application.start())
        loop.run_until_complete(application.updater.start_polling())
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Application received stop signal. Shutting down.")
    finally:
        if application is not None:
            try:
                if application.updater.running:
                    loop.run_until_complete(application.updater.stop())
            except Exception:
                logger.exception("Failed to stop updater")
            try:
                if application.running:
                    loop.run_until_complete(application.stop())
                    loop.run_until_complete(application.shutdown())
            except Exception:
                logger.exception("Failed to stop application")
        loop.run_until_complete(db.dispose())
        loop.close()


def worker() -> None:
    """Initialize the worker process: configuration and logging only.

    The worker service does not interact with Telegram, so it does not require
    BOT_TOKEN. RSS polling, summarization, and scheduled delivery run here.
    """
    settings = Settings()
    setup_logging(settings.log_level)
    logger.info("worker process starting (app_env=%s)", settings.app_env)

    db = Database(settings.database_url)
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
    fetch_full_text = os.environ.get("FETCH_FULL_TEXT", "true").lower() == "true"
    fetch_timeout = int(os.environ.get("FETCH_TIMEOUT_SECONDS", "30"))

    news_worker = NewsWorker(
        engine=db.engine,
        poll_interval_seconds=poll_interval,
        fetch_full_text=fetch_full_text,
        fetch_timeout=fetch_timeout,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.add_signal_handler(
            signal.SIGTERM, lambda: asyncio.ensure_future(news_worker.stop())
        )
        loop.add_signal_handler(
            signal.SIGINT, lambda: asyncio.ensure_future(news_worker.stop())
        )
    except (NotImplementedError, RuntimeError):
        # Signal handlers are not supported on this platform (e.g., Windows).
        # The worker will still run; it just can't be stopped via signals.
        logger.warning("Signal handlers not supported on this platform")

    try:
        loop.run_until_complete(news_worker.run())
    finally:
        loop.run_until_complete(db.dispose())
        loop.close()
