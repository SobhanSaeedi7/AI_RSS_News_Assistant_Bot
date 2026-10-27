"""Telegram command handlers.

Each handler turns an update into one use-case call and renders the outcome with
a fixed message from :mod:`ai_rss_news_assistant.bot.messages`. No handler
issues SQL, calls the LLM, or puts an exception or credential into user-visible
text.

Everything that comes from a feed, an article, or an answer is untrusted: it is
truncated and HTML-escaped before being sent with ``parse_mode="HTML"``, so
stored content cannot inject markup or break a message, and only ``http(s)``
links are rendered as links.
"""

from __future__ import annotations

import html
import logging
import uuid
from urllib.parse import urlsplit

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ai_rss_news_assistant.bot import use_cases
from ai_rss_news_assistant.bot.messages import (
    ADD_FEED_DUPLICATE,
    ADD_FEED_INVALID,
    ADD_FEED_SUCCESS,
    ADD_FEED_USAGE,
    ASK_MAX_MESSAGE_LENGTH,
    ASK_MAX_QUESTION_LENGTH,
    ASK_MAX_SOURCE_TITLE_LENGTH,
    ASK_TOO_LONG,
    ASK_USAGE,
    DEL_FEED_NOT_FOUND,
    DEL_FEED_SUCCESS,
    DEL_FEED_USAGE,
    DELIVERY_OFF,
    DELIVERY_ON,
    DELIVERY_STATUS_OFF,
    DELIVERY_STATUS_ON,
    DELIVERY_USAGE,
    HELP_MESSAGE,
    LIST_FEED_ITEM,
    LIST_FEEDS_EMPTY,
    LIST_FEEDS_HEADER,
    LIST_FEEDS_MAX_MESSAGE_LENGTH,
    NEWS_EMPTY,
    NEWS_HEADER,
    NEWS_LIMIT,
    NEWS_MAX_MESSAGE_LENGTH,
    NEWS_MAX_SOURCE_LENGTH,
    NEWS_MAX_TITLE_LENGTH,
    NEWS_MAX_URL_LENGTH,
    WELCOME_MESSAGE,
)
from ai_rss_news_assistant.contracts import AGENT_ERROR, AgentAnswer, AgentSource
from ai_rss_news_assistant.db.base import Database
from ai_rss_news_assistant.db.models import NewsItem, UserFeed
from ai_rss_news_assistant.urls import safe_url

logger = logging.getLogger(__name__)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start: find or create user, then welcome them."""
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    db: Database = context.application.bot_data["db"]
    await use_cases.ensure_user_id(db, telegram_user.id)

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

    db: Database = context.application.bot_data["db"]
    outcome = await use_cases.add_feed(db, telegram_user.id, args[0])

    if outcome.status is use_cases.FeedAddStatus.INVALID:
        await update.effective_message.reply_text(ADD_FEED_INVALID)
        return
    if outcome.status is use_cases.FeedAddStatus.DUPLICATE:
        await update.effective_message.reply_text(ADD_FEED_DUPLICATE)
        return

    feed = outcome.feed
    await update.effective_message.reply_text(
        ADD_FEED_SUCCESS.format(
            url=feed.url if feed is not None else args[0],
            title=(feed.title if feed is not None else None) or "(بدون عنوان)",
        )
    )


async def list_feeds_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /listfeeds: list current user's feeds."""
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    db: Database = context.application.bot_data["db"]
    feeds = await use_cases.list_user_feeds(db, telegram_user.id)

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
    deleted = await use_cases.remove_feed(db, telegram_user.id, feed_id)

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
    items = await use_cases.latest_news(db, telegram_user.id, limit=NEWS_LIMIT)

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
    if args:
        enabled = await use_cases.set_delivery_preference(
            db, telegram_user.id, args[0] == "on"
        )
    else:
        enabled = await use_cases.delivery_preference(db, telegram_user.id)

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
    answerer = context.application.bot_data["agent"]

    answer = await use_cases.answer_question(
        db, answerer, telegram_user.id, question
    )

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
