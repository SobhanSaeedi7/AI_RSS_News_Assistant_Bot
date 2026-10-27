"""User-facing Telegram texts for the bot.

Every string a user can see lives here so wording stays consistent and can be
reviewed in one place. Nothing in this module imports the database, the LLM, or
Telegram, and nothing here interpolates an exception or credential: internal
failures are reported with a fixed Persian message, never with implementation
detail.

Messages that describe an answer outcome (insufficient evidence, agent
unavailable, generic failure) are part of the cross-service contract and live in
:mod:`ai_rss_news_assistant.contracts` instead, so the bot and the agent cannot
drift apart.
"""

from __future__ import annotations

from ai_rss_news_assistant.contracts import MAX_QUESTION_CHARS

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

ADD_FEED_USAGE = "لطفاً یک آدرس فید RSS ارائه دهید:\n/addfeed <url>"

ADD_FEED_INVALID = "آدرس feed نامعتبر است. لطفاً یک URL معتبر (http/https) ارائه دهید."

ADD_FEED_SUCCESS = "فید با موفقیت اضافه شد.\nآدرس: {url}\nعنوان: {title}"

ADD_FEED_DUPLICATE = "این فید قبلاً برای شما اضافه شده است."

LIST_FEEDS_EMPTY = (
    "هیچ فیدی ثبت نشده است. از /addfeed برای اضافه کردن فید استفاده کنید."
)

LIST_FEEDS_HEADER = "فیدهای شما:\n\n"
# Telegram rejects messages longer than 4096 characters; leave room for safety.
LIST_FEEDS_MAX_MESSAGE_LENGTH = 4000

LIST_FEED_ITEM = "{id}. {title} — {url} ({status})\n"

DEL_FEED_USAGE = "لطفاً شناسه فید را ارائه دهید:\n/delfeed <id>"

DEL_FEED_SUCCESS = "فید با شناسه {id} حذف شد."

DEL_FEED_NOT_FOUND = "فیدی با شناسه {id} یافت نشد یا دسترسی به آن ندارید."

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
# The limit is part of the agent contract: the HTTP API rejects longer questions.
ASK_MAX_QUESTION_LENGTH = MAX_QUESTION_CHARS
# Telegram rejects messages longer than 4096 characters; keep a safety margin.
ASK_MAX_MESSAGE_LENGTH = 4000
ASK_MAX_SOURCE_TITLE_LENGTH = 200
