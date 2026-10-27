"""Application entry point shared by the bot and worker processes.

This module only wires up configuration and logging. The bot (Telegram
interaction) and worker (RSS polling, summarization, delivery) behaviors
are implemented here.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import signal
import uuid
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError
from telegram import Bot, Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from ai_rss_news_assistant.agent import (
    AGENT_ERROR,
    AgentAnswer,
    AgentSource,
    NewsAgent,
    safe_url,
)
from ai_rss_news_assistant.config import Settings
from ai_rss_news_assistant.db.base import Database
from ai_rss_news_assistant.db.models import NewsItem, UserFeed
from ai_rss_news_assistant.db.repository import (
    create_feed,
    create_user,
    delete_feed,
    get_user_by_telegram_id,
    list_feeds,
    list_news_items_with_feed,
    update_user_settings,
)
from ai_rss_news_assistant.db.url_normalization import normalize_url
from ai_rss_news_assistant.llm import build_chat_client
from ai_rss_news_assistant.logging import setup_logging
from ai_rss_news_assistant.worker.delivery import DeliveryService
from ai_rss_news_assistant.worker.service import NewsWorker
from ai_rss_news_assistant.worker.summarization import build_summarizer

logger = logging.getLogger(__name__)

WELCOME_MESSAGE = (
    "سلام! به ربات خبری RSS خوش آمدید.\n\n"
    "دستورهای موجود:\n"
    "/start - ثبت نام و شروع کار با ربات\n"
    "/help - نمایش راهنما\n"
    "/addfeed <url> - اضافه کردن فید RSS جدید\n"
    "/listfeeds - مشاهده فیدهای شما\n"
    "/delfeed <id> - حذف یک فید\n"
    "/news - نمایش آخرین اخبار ذخیره‌شده\n"
    "/delivery on|off - روشن یا خاموش کردن ارسال خودکار\n"
    "/ask <سؤال> - پرسیدن سؤال از خبرهای ذخیره‌شده شما"
)

HELP_MESSAGE = (
    "راهنما ربات خبری RSS\n\n"
    "دستورهای ممکن:\n"
    "/start - ثبت نام و شروع کار با ربات\n"
    "/help - نمایش راهنما\n"
    "/addfeed <url> - اضافه کردن فید RSS جدید\n"
    "/listfeeds - مشاهده فیدهای شما\n"
    "/delfeed <id> - حذف یک فید\n"
    "/news - نمایش آخرین اخبار ذخیره‌شده\n"
    "/delivery on|off - روشن یا خاموش کردن ارسال خودکار\n"
    "/ask <سؤال> - پرسیدن سؤال از خبرهای ذخیره‌شده شما"
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
LIST_FEEDS_MAX_MESSAGE_LENGTH = 4000

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

NEWS_LIMIT = 5
# Telegram rejects messages longer than 4096 characters; leave room for the header.
NEWS_MAX_MESSAGE_LENGTH = 4000
NEWS_MAX_TITLE_LENGTH = 300
NEWS_MAX_SOURCE_LENGTH = 100
NEWS_MAX_URL_LENGTH = 1000

NEWS_EMPTY = (
    "هنوز خبری برای شما ذخیره نشده است.\n"
    "با /addfeed یک فید اضافه کنید و کمی صبر کنید تا اخبار دریافت شوند."
)

NEWS_HEADER = "آخرین اخبار شما:\n\n"

DELIVERY_USAGE = (
    "برای تغییر وضعیت ارسال خودکار:\n"
    "/delivery on - روشن کردن ارسال خودکار\n"
    "/delivery off - خاموش کردن ارسال خودکار\n"
    "/delivery - مشاهده وضعیت فعلی"
)

DELIVERY_ON = "ارسال خودکار خبرها روشن شد."
DELIVERY_OFF = "ارسال خودکار خبرها خاموش شد."

DELIVERY_STATUS_ON = "وضعیت ارسال خودکار: روشن ✅"
DELIVERY_STATUS_OFF = "وضعیت ارسال خودکار: خاموش ⛔"

ASK_USAGE = (
    "برای پرسیدن سؤال درباره خبرهای ذخیره‌شده:\n"
    "/ask <سؤال>\n\n"
    "مثال:\n"
    "/ask مهم‌ترین خبرهای فناوری این هفته چه بود؟"
)
ASK_TOO_LONG = "سؤال شما طولانی است. لطفاً آن را کوتاه‌تر مطرح کنید."
ASK_MAX_QUESTION_LENGTH = 500
# Telegram rejects messages longer than 4096 characters; keep a safety margin.
ASK_MAX_MESSAGE_LENGTH = 4000
ASK_MAX_SOURCE_TITLE_LENGTH = 200


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

    # Parse before validation so malformed IPv6 input is handled as an invalid
    # URL instead of surfacing a ValueError. Reject non-http(s) schemes and
    # embedded credentials before normalization; credentials could otherwise
    # be stored and appear in logs.
    try:
        parts = urlsplit(url)
    except ValueError:
        await update.effective_message.reply_text(ADD_FEED_INVALID)
        return

    if (
        parts.scheme.lower() not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
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

    for message in _split_message("".join(lines), LIST_FEEDS_MAX_MESSAGE_LENGTH):
        await update.effective_message.reply_text(message)


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


def _truncate(value: str, limit: int) -> str:
    """Trim untrusted text before escaping so entities are never cut in half."""
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)].rstrip() + "…"


def _feed_display_name(feed: UserFeed) -> str:
    """Prefer the feed title, falling back to the feed URL's hostname."""
    if feed.title:
        return feed.title
    try:
        return urlsplit(feed.url).hostname or feed.url
    except ValueError:
        return feed.url


def _format_news_item(index: int, item: NewsItem, feed: UserFeed) -> str:
    """Format one stored article as an HTML-mode Telegram block."""
    raw_title = item.title.strip() if item.title else ""
    title = (
        html.escape(_truncate(raw_title, NEWS_MAX_TITLE_LENGTH), quote=False)
        or "(بدون عنوان)"
    )
    source = html.escape(
        _truncate(_feed_display_name(feed), NEWS_MAX_SOURCE_LENGTH), quote=False
    )
    url = html.escape(_truncate(item.url, NEWS_MAX_URL_LENGTH), quote=False)

    lines = [f"{index}. <b>{title}</b>", f"منبع: {source}"]
    if item.published_at is not None:
        lines.append(f"تاریخ انتشار: {item.published_at:%Y-%m-%d}")
    lines.append(f"لینک: {url}")
    return "\n".join(lines)


def _build_news_messages(items: list[tuple[NewsItem, UserFeed]]) -> list[str]:
    """Split formatted articles into Telegram-safe message chunks."""
    messages: list[str] = []
    current = NEWS_HEADER
    for index, (item, feed) in enumerate(items, start=1):
        block = _format_news_item(index, item, feed) + "\n\n"
        if len(current) + len(block) > NEWS_MAX_MESSAGE_LENGTH:
            messages.append(current.rstrip())
            current = block
        else:
            current += block
    messages.append(current.rstrip())
    return messages


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /news: show the latest stored articles owned by this user."""
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    db: Database = context.application.bot_data["db"]
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user.id)
        items = (
            await list_news_items_with_feed(session, user.id, limit=NEWS_LIMIT)
            if user is not None
            else []
        )

    if not items:
        await update.effective_message.reply_text(NEWS_EMPTY)
        return

    for message in _build_news_messages(items):
        await update.effective_message.reply_text(message, parse_mode="HTML")


async def delivery_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /delivery [on|off]: show or update the delivery preference."""
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    args = [argument.lower() for argument in context.args]
    if len(args) > 1 or (args and args[0] not in ("on", "off")):
        await update.effective_message.reply_text(DELIVERY_USAGE)
        return

    db: Database = context.application.bot_data["db"]
    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if user is None:
            user = await create_user(session, telegram_user.id)

        if args:
            enabled = args[0] == "on"
            await update_user_settings(session, user.id, auto_delivery_enabled=enabled)
            user.auto_delivery_enabled = enabled

        enabled = user.auto_delivery_enabled

    if args:
        message = DELIVERY_ON if enabled else DELIVERY_OFF
    else:
        message = DELIVERY_STATUS_ON if enabled else DELIVERY_STATUS_OFF
    await update.effective_message.reply_text(message)


def _entity_safe_cut(text: str, limit: int) -> int:
    """Back off a hard cut so an ``&...;`` HTML entity is not split."""
    ampersand = text.rfind("&", 0, limit)
    if ampersand != -1 and ";" not in text[ampersand:limit]:
        return ampersand or limit
    return limit


def _split_message(text: str, limit: int) -> list[str]:
    """Split text on word boundaries so HTML entities are never cut in half."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(word) > limit:
            cut = _entity_safe_cut(word, limit)
            chunks.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]


def _format_sources(sources: list[AgentSource]) -> str:
    """Render source titles as clickable links, escaping all stored text."""
    lines = ["منابع:"]
    for index, source in enumerate(sources, start=1):
        title = html.escape(
            _truncate(source.title, ASK_MAX_SOURCE_TITLE_LENGTH), quote=False
        )
        url = safe_url(source.url)
        if url is not None and len(url) <= 2000:
            href = html.escape(url, quote=True)
            lines.append(f'[{index}] <a href="{href}">{title}</a>')
        else:
            lines.append(f"[{index}] {title}")
    return "\n".join(lines)


def build_ask_messages(answer: AgentAnswer) -> list[str]:
    """Render an agent answer and its sources as Telegram-safe chunks."""
    text = answer.text.strip() or AGENT_ERROR
    messages = _split_message(html.escape(text, quote=False), ASK_MAX_MESSAGE_LENGTH)
    if answer.grounded and answer.sources:
        sources = _format_sources(answer.sources)
        if len(messages[-1]) + 2 + len(sources) <= ASK_MAX_MESSAGE_LENGTH:
            messages[-1] = f"{messages[-1]}\n\n{sources}"
        else:
            messages.extend(_split_message(sources, ASK_MAX_MESSAGE_LENGTH))
    return messages


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ask <question>: answer from this user's stored news only."""
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    question = " ".join(context.args).strip()
    if not question:
        await update.effective_message.reply_text(ASK_USAGE)
        return
    if len(question) > ASK_MAX_QUESTION_LENGTH:
        await update.effective_message.reply_text(ASK_TOO_LONG)
        return

    db: Database = context.application.bot_data["db"]
    agent: NewsAgent = context.application.bot_data["agent"]

    async with db.session() as session:
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if user is None:
            user = await create_user(session, telegram_user.id)
        user_id = user.id

    try:
        answer = await agent.answer(user_id, question)
    except Exception:  # a handler must never crash polling
        logger.exception("Agent failed to answer a question")
        answer = AgentAnswer(text=AGENT_ERROR)

    for message in build_ask_messages(answer):
        try:
            await update.effective_message.reply_text(
                message, parse_mode="HTML", disable_web_page_preview=True
            )
        except TelegramError:
            logger.warning(
                "Failed to send an agent answer to Telegram user %s",
                telegram_user.id,
            )
            break


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
        application.bot_data["agent"] = NewsAgent(
            session_factory=db.session_factory,
            client=build_chat_client(settings),
        )

        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("addfeed", add_feed_command))
        application.add_handler(CommandHandler("listfeeds", list_feeds_command))
        application.add_handler(CommandHandler("delfeed", delete_feed_command))
        application.add_handler(CommandHandler("news", news_command))
        application.add_handler(CommandHandler("delivery", delivery_command))
        application.add_handler(CommandHandler("ask", ask_command))
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
    except InvalidToken:
        # InvalidToken messages can contain the rejected token; log a fixed
        # message instead of the exception text or traceback.
        logger.error("Telegram rejected BOT_TOKEN; the bot cannot start")
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
    """Initialize the worker process: ingestion, summarization, and delivery.

    The worker sends automatic delivery messages, so it uses ``BOT_TOKEN`` when
    it is configured. Without a token it still ingests news; delivery is
    disabled and a warning is logged.
    """
    settings = Settings()
    setup_logging(settings.log_level)
    logger.info("worker process starting (app_env=%s)", settings.app_env)

    db = Database(settings.database_url)
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
    fetch_full_text = os.environ.get("FETCH_FULL_TEXT", "true").lower() == "true"
    fetch_timeout = int(os.environ.get("FETCH_TIMEOUT_SECONDS", "30"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    bot: Bot | None = None
    if settings.bot_token is not None:
        try:
            bot = Bot(settings.bot_token)
        except InvalidToken:
            # The exception message contains the rejected token; never log it.
            logger.error("Telegram rejected BOT_TOKEN; automatic delivery is disabled")
            bot = None
        else:
            try:
                loop.run_until_complete(bot.initialize())
            except InvalidToken:
                # The exception message contains the rejected token; never log it.
                logger.error(
                    "Telegram rejected BOT_TOKEN; automatic delivery is disabled"
                )
                try:
                    loop.run_until_complete(bot.shutdown())
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    logger.debug("Telegram bot cleanup after invalid token failed")
                bot = None
            except Exception:  # noqa: BLE001 - connectivity failures must not stop ingestion
                # Connectivity problems must not stop RSS ingestion. Delivery
                # attempts fail per item and are retried on the next poll.
                logger.warning(
                    "Could not initialize the Telegram bot; delivery attempts will "
                    "retry on each poll"
                )
    else:
        logger.warning("BOT_TOKEN is not set; automatic delivery is disabled")

    delivery_service = (
        DeliveryService(
            session_factory=db.session_factory,
            bot=bot,
            summarizer=build_summarizer(settings),
        )
        if bot is not None
        else None
    )

    news_worker = NewsWorker(
        engine=db.engine,
        poll_interval_seconds=poll_interval,
        fetch_full_text=fetch_full_text,
        fetch_timeout=fetch_timeout,
        delivery_service=delivery_service,
    )

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
        if bot is not None:
            try:
                loop.run_until_complete(bot.shutdown())
            except Exception:
                logger.exception("Failed to shut down the Telegram bot")
        loop.run_until_complete(db.dispose())
        loop.close()
