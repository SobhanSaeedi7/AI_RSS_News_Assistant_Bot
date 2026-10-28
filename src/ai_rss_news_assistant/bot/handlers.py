"""Telegram command handlers.

Each handler turns an update into one use-case call and renders the outcome with
a fixed message from :mod:`ai_rss_news_assistant.bot.messages`. No handler
issues SQL, calls the LLM, or puts an exception or credential into user-visible
text.

Everything that comes from a feed, an article, or an answer is untrusted: it is
truncated and HTML-escaped before being sent with ``parse_mode="HTML"``, so
stored content cannot inject markup or break a message, and only ``http(s)``
links are rendered as links.

``/addfeed`` and ``/delfeed`` also support a two-step form. The pending action is
kept in ``context.user_data`` -- which Telegram scopes to the user -- and is
further tagged with the chat id and an expiry, so a prompt in one chat can never
consume a message from another chat or from a later session.
"""

from __future__ import annotations

import html
import logging
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ai_rss_news_assistant.bot import use_cases
from ai_rss_news_assistant.bot.messages import (
    ADD_FEED_BLOCKED,
    ADD_FEED_DUPLICATE,
    ADD_FEED_EMPTY,
    ADD_FEED_INVALID,
    ADD_FEED_NOT_A_FEED,
    ADD_FEED_PROMPT,
    ADD_FEED_SUCCESS,
    ADD_FEED_UNREACHABLE,
    ADD_FEED_USAGE,
    ASK_MAX_MESSAGE_LENGTH,
    ASK_MAX_QUESTION_LENGTH,
    ASK_MAX_SOURCE_TITLE_LENGTH,
    ASK_TOO_LONG,
    ASK_USAGE,
    CANCEL_DONE,
    CANCEL_NOTHING,
    DEL_FEED_NOT_FOUND,
    DEL_FEED_PROMPT,
    DEL_FEED_SUCCESS,
    DEL_FEED_SUCCESS_URL,
    DEL_FEED_USAGE,
    DELIVERY_OFF,
    DELIVERY_ON,
    DELIVERY_USAGE,
    FEED_ERROR_LABELS,
    HELP_MESSAGE,
    LIST_FEED_FAILING_STATUS,
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
    PENDING_EXPIRED,
    SCHEDULE_STATUS,
    SCHEDULE_UPDATED,
    SCHEDULE_USAGE,
    WELCOME_MESSAGE,
)
from ai_rss_news_assistant.bot.schedule import format_interval, parse_interval
from ai_rss_news_assistant.config import DEFAULT_FETCH_TIMEOUT_SECONDS
from ai_rss_news_assistant.contracts import AGENT_ERROR, AgentAnswer, AgentSource
from ai_rss_news_assistant.db.base import Database
from ai_rss_news_assistant.db.models import NewsItem, UserFeed
from ai_rss_news_assistant.urls import safe_url

logger = logging.getLogger(__name__)

# Two-step feed interactions. The key is intentionally namespaced so it cannot
# collide with a future use of ``context.user_data``.
PENDING_FEED_KEY = "pending_feed_action"
PENDING_FEED_TTL_SECONDS = 300
_PENDING_ADD = "add"
_PENDING_DELETE = "delete"


def _chat_id(update: Update) -> int | None:
    """Return the chat id of the update, if Telegram supplied one."""
    message = update.effective_message
    if message is None:
        return None
    return getattr(message, "chat_id", None)


def _set_pending(
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    telegram_user_id: int,
    chat_id: int | None,
) -> None:
    """Remember a two-step interaction for this user and chat."""
    user_data = context.user_data
    if not isinstance(user_data, dict):
        # PTB always provides a dict in production; if a test double does not,
        # there is nothing safe to persist.
        return
    user_data[PENDING_FEED_KEY] = {
        "action": action,
        "telegram_user_id": telegram_user_id,
        "chat_id": chat_id,
        "expires_at": datetime.now(UTC) + timedelta(seconds=PENDING_FEED_TTL_SECONDS),
    }


def _take_pending(
    context: ContextTypes.DEFAULT_TYPE,
    telegram_user_id: int,
    chat_id: int | None,
) -> tuple[dict | None, bool]:
    """Consume the pending action and report whether it had already expired.

    The state is removed in every case, so a malformed, cross-chat, or expired
    value cannot be reused. ``(None, True)`` tells the caller to show the expiry
    notice; ``(None, False)`` means there was no matching pending action.
    """
    user_data = context.user_data
    if not isinstance(user_data, dict):
        return None, False
    pending = user_data.pop(PENDING_FEED_KEY, None)
    if not isinstance(pending, dict):
        return None, False
    if (
        pending.get("telegram_user_id") != telegram_user_id
        or pending.get("chat_id") != chat_id
    ):
        return None, False
    expires_at = pending.get("expires_at")
    if not isinstance(expires_at, datetime) or expires_at <= datetime.now(UTC):
        return None, True
    return pending, False


def _clear_pending(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Drop a pending interaction; return whether one was present."""
    user_data = context.user_data
    if not isinstance(user_data, dict):
        return False
    return user_data.pop(PENDING_FEED_KEY, None) is not None


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


async def _process_add_feed(
    update: Update, context: ContextTypes.DEFAULT_TYPE, raw_url: str
) -> None:
    """Validate, probe, and store one feed URL, then render the outcome."""
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    db: Database = context.application.bot_data["db"]
    # The probe timeout is configured, not hard-coded: the bot reaches this
    # through the same FETCH_TIMEOUT_SECONDS setting the worker uses.
    fetch_timeout = context.application.bot_data.get(
        "fetch_timeout_seconds", DEFAULT_FETCH_TIMEOUT_SECONDS
    )
    outcome = await use_cases.add_feed(
        db, telegram_user.id, raw_url, fetch_timeout=fetch_timeout
    )

    rejection = {
        use_cases.FeedAddStatus.INVALID: ADD_FEED_INVALID,
        use_cases.FeedAddStatus.BLOCKED: ADD_FEED_BLOCKED,
        use_cases.FeedAddStatus.UNREACHABLE: ADD_FEED_UNREACHABLE,
        use_cases.FeedAddStatus.NOT_A_FEED: ADD_FEED_NOT_A_FEED,
        use_cases.FeedAddStatus.DUPLICATE: ADD_FEED_DUPLICATE,
    }.get(outcome.status)
    if rejection is not None:
        await update.effective_message.reply_text(rejection)
        return

    feed = outcome.feed
    template = (
        ADD_FEED_EMPTY
        if outcome.status is use_cases.FeedAddStatus.EMPTY
        else ADD_FEED_SUCCESS
    )
    await update.effective_message.reply_text(
        template.format(
            url=feed.url if feed is not None else raw_url,
            title=(feed.title if feed is not None else None) or "(بدون عنوان)",
        )
    )


async def add_feed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /addfeed [url]: add a feed directly or start the two-step flow."""
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    args = context.args or []
    if len(args) > 1:
        await update.effective_message.reply_text(ADD_FEED_USAGE)
        return
    if args:
        _clear_pending(context)
        await _process_add_feed(update, context, args[0])
        return

    _set_pending(context, _PENDING_ADD, telegram_user.id, _chat_id(update))
    await update.effective_message.reply_text(ADD_FEED_PROMPT)


async def list_feeds_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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
        status = _feed_status_label(feed)
        title = feed.title or "(بدون عنوان)"
        lines.append(
            LIST_FEED_ITEM.format(id=feed.id, title=title, url=feed.url, status=status)
        )

    for message in _split_message("".join(lines), LIST_FEEDS_MAX_MESSAGE_LENGTH):
        await update.effective_message.reply_text(message)


async def _process_delete_feed(
    update: Update, context: ContextTypes.DEFAULT_TYPE, identifier: str
) -> None:
    """Delete one feed by UUID or by its exact URL, scoped to the caller."""
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    value = identifier.strip()
    db: Database = context.application.bot_data["db"]

    try:
        feed_id = uuid.UUID(value)
    except (ValueError, AttributeError):
        feed_id = None

    if feed_id is not None:
        deleted = await use_cases.remove_feed(db, telegram_user.id, feed_id)
        if deleted:
            await update.effective_message.reply_text(
                DEL_FEED_SUCCESS.format(id=feed_id)
            )
        else:
            await update.effective_message.reply_text(
                DEL_FEED_NOT_FOUND.format(id=feed_id)
            )
        return

    if value.lower().startswith(("http://", "https://")):
        deleted = await use_cases.remove_feed_by_url(db, telegram_user.id, value)
        if deleted:
            await update.effective_message.reply_text(
                DEL_FEED_SUCCESS_URL.format(url=value)
            )
        else:
            await update.effective_message.reply_text(
                DEL_FEED_NOT_FOUND.format(id=value)
            )
        return

    await update.effective_message.reply_text(DEL_FEED_NOT_FOUND.format(id=value))


async def delete_feed_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /delfeed [id|url]: delete directly or start the two-step flow."""
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    args = context.args or []
    if len(args) > 1:
        await update.effective_message.reply_text(DEL_FEED_USAGE)
        return
    if args:
        _clear_pending(context)
        await _process_delete_feed(update, context, args[0])
        return

    _set_pending(context, _PENDING_DELETE, telegram_user.id, _chat_id(update))
    await update.effective_message.reply_text(DEL_FEED_PROMPT)


async def pending_feed_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle the next text message for a pending /addfeed or /delfeed.

    A message with no pending action is ignored, so ordinary chat is never
    consumed by an old prompt. The state is scoped to the Telegram user and the
    chat, and it is removed before any network or database work.
    """
    telegram_user = update.effective_user
    message = update.effective_message
    if telegram_user is None or message is None:
        return

    text = message.text
    if not isinstance(text, str) or not text.strip():
        return

    pending, expired = _take_pending(context, telegram_user.id, _chat_id(update))
    if expired:
        await message.reply_text(PENDING_EXPIRED)
        return
    if pending is None:
        return

    action = pending.get("action")
    if action == _PENDING_ADD:
        await _process_add_feed(update, context, text.strip())
    elif action == _PENDING_DELETE:
        await _process_delete_feed(update, context, text.strip())


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel: discard a pending /addfeed or /delfeed interaction."""
    if update.effective_message is None:
        return
    message = CANCEL_DONE if _clear_pending(context) else CANCEL_NOTHING
    await update.effective_message.reply_text(message)


async def mark_recipient_available_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Clear a persisted "recipient unavailable" status on any new update.

    Telegram only proves it can reach a user when that user contacts the bot
    again. This runs before the command handlers (group ``-1``) and never
    replies, so it cannot interfere with the command's own response.
    """
    telegram_user = update.effective_user
    if telegram_user is None:
        return
    db = context.application.bot_data.get("db")
    if db is None:
        return
    try:
        await use_cases.mark_recipient_available(db, telegram_user.id)
    except Exception:
        # Coordination state must never break an otherwise valid command.
        logger.exception(
            "Failed to clear the unavailable status for Telegram user %s",
            telegram_user.id,
        )


def _truncate(value: str, limit: int) -> str:
    """Trim untrusted text before escaping so entities are never cut in half."""
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)].rstrip() + "…"


def _feed_status_label(feed: UserFeed) -> str:
    """Render a feed's active/inactive and health state for /listfeeds.

    Only the fixed error categories stored on the row are shown, never raw
    exception text, and an unknown/missing value simply renders as healthy.
    """
    if not feed.active:
        return "غیرفعال"
    if not feed.last_error:
        return "فعال"
    reason = FEED_ERROR_LABELS.get(str(feed.last_error))
    if reason is None:
        return "فعال"
    return LIST_FEED_FAILING_STATUS.format(reason=reason)


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

    try:
        for message in _build_news_messages(items):
            await update.effective_message.reply_text(message, parse_mode="HTML")
    except TelegramError:
        # A Telegram failure must not escape the handler; the next /news retries.
        logger.warning("Failed to send news to Telegram user %s", telegram_user.id)


async def delivery_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /delivery: toggle automatic delivery without arguments.

    The previous ``/delivery on|off`` form was removed because it duplicated the
    persistent switch with an argument that could be forgotten. Extra arguments
    are rejected explicitly instead of being interpreted as a preference.
    """
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    if context.args:
        await update.effective_message.reply_text(DELIVERY_USAGE)
        return

    db: Database = context.application.bot_data["db"]
    enabled = await use_cases.toggle_delivery_preference(db, telegram_user.id)
    await update.effective_message.reply_text(DELIVERY_ON if enabled else DELIVERY_OFF)


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /schedule [interval]: show or change the delivery cadence.

    The interval is a minimum spacing between two automatic deliveries. It is
    independent of ``/delivery`` (the ON/OFF switch), which stops automatic
    delivery entirely, and it never affects ``/news`` or ``/ask``.
    """
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return

    args = [argument.strip().lower() for argument in (context.args or [])]

    if len(args) > 1:
        await update.effective_message.reply_text(SCHEDULE_USAGE)
        return

    db: Database = context.application.bot_data["db"]

    if args:
        requested = parse_interval(args[0])
        if requested is None:
            await update.effective_message.reply_text(SCHEDULE_USAGE)
            return
        await use_cases.set_delivery_interval(db, telegram_user.id, requested)
        await update.effective_message.reply_text(
            SCHEDULE_UPDATED.format(interval=format_interval(requested))
        )
        return

    current = await use_cases.delivery_interval(db, telegram_user.id)
    await update.effective_message.reply_text(
        SCHEDULE_STATUS.format(interval=format_interval(current))
    )


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
    """Render source titles as clickable links, escaping all stored text.

    The number in front of each source is the citation number the agent used in
    the answer, not its position in this list, so ``[2]`` in the answer always
    points at the source labelled ``[2]``.
    """
    lines = ["منابع:"]
    for position, source in enumerate(sources, start=1):
        number = (
            source.citation_index if source.citation_index is not None else position
        )
        title = html.escape(
            _truncate(source.title, ASK_MAX_SOURCE_TITLE_LENGTH), quote=False
        )
        url = safe_url(source.url)
        if url is not None and len(url) <= 2000:
            href = html.escape(url, quote=True)
            lines.append(f'[{number}] <a href="{href}">{title}</a>')
        else:
            lines.append(f"[{number}] {title}")
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

    question = " ".join(context.args or []).strip()
    if not question:
        await update.effective_message.reply_text(ASK_USAGE)
        return
    if len(question) > ASK_MAX_QUESTION_LENGTH:
        await update.effective_message.reply_text(ASK_TOO_LONG)
        return

    db: Database = context.application.bot_data["db"]
    answerer = context.application.bot_data["agent"]

    answer = await use_cases.answer_question(db, answerer, telegram_user.id, question)

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
