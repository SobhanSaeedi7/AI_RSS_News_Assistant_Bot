"""User-facing Telegram texts for the bot.

Every string a user can see lives here so wording stays consistent and can be
reviewed in one place. Nothing in this module imports the database, the LLM, or
Telegram, and nothing here interpolates an exception or credential: internal
failures are reported with a fixed Persian message, never with implementation
detail.

The schedule text is derived from :func:`bot.schedule.interval_options`, so the
commands a user reads in ``/start`` and ``/help`` can never drift from the
values the parser actually accepts.

Messages that describe an answer outcome (insufficient evidence, agent
unavailable, generic failure) are part of the cross-service contract and live in
:mod:`ai_rss_news_assistant.contracts` instead, so the bot and the agent cannot
drift apart.
"""

from __future__ import annotations

from ai_rss_news_assistant.bot.schedule import interval_options
from ai_rss_news_assistant.contracts import MAX_QUESTION_CHARS

# The one authoritative list of accepted cadences, reused by every message.
SCHEDULE_OPTIONS = interval_options()

DELIVERY_COMMAND_HINT = (
    "/delivery - روشن/خاموش کردن ارسال خودکار (بدون آرگومان؛ هر بار تغییر می‌کند)"
)
CANCEL_COMMAND_HINT = "/cancel - لغو درخواست نیمه‌تمام افزودن یا حذف فید"

_COMMAND_LIST = (
    "/start - ثبت نام و شروع کار با ربات\n"
    "/help - نمایش راهنما\n"
    "/addfeed [url] - افزودن فید RSS (بدون url، ربات آدرس را می‌پرسد)\n"
    "/listfeeds - مشاهده فیدهای شما\n"
    "/delfeed [id|url] - حذف فید (بدون شناسه، ربات می‌پرسد)\n"
    "/news - نمایش آخرین اخبار ذخیره‌شده\n"
    f"{DELIVERY_COMMAND_HINT}\n"
    f"/schedule <بازه> - تنظیم فاصله ارسال خودکار خبر ({SCHEDULE_OPTIONS})\n"
    f"{CANCEL_COMMAND_HINT}\n"
    "/ask <سؤال> - پرسیدن سؤال از خبرهای ذخیره‌شده شما"
)

WELCOME_MESSAGE = (
    "سلام! به ربات خبری RSS خوش آمدید.\n\nدستورهای موجود:\n" + _COMMAND_LIST
)

# Kept as a separate constant so /help can explain the two settings without
# repeating the command list.
SCHEDULE_COMMAND_HINT = (
    f"/schedule <بازه> - تنظیم فاصله ارسال خودکار خبر ({SCHEDULE_OPTIONS})"
)

HELP_MESSAGE = (
    "راهنما ربات خبری RSS\n\n"
    "دستورهای ممکن:\n"
    f"{_COMMAND_LIST}\n\n"
    "نکته‌ها:\n"
    "• بازه schedule فقط روی ارسال خودکار اثر دارد؛ /news و /ask همیشه در دسترس‌اند.\n"
    "• مقدار 0 در /schedule یعنی بدون محدودیت (فوری).\n"
    "• /delivery بدون آرگومان وضعیت ارسال خودکار را جابه‌جا می‌کند؛ آرگومان اضافه نمی‌پذیرد.\n"
    "• /addfeed و /delfeed را می‌توانید بدون آرگومان بفرستید؛ ربات مقدار لازم را در پیام بعدی می‌پرسد.\n"
    "• درخواست نیمه‌تمام پس از ۵ دقیقه منقضی می‌شود و با /cancel هم لغو می‌شود."
)

ADD_FEED_USAGE = "لطفاً یک آدرس فید RSS ارائه دهید:\n/addfeed <url>"

ADD_FEED_PROMPT = (
    f"{ADD_FEED_USAGE}\n\n"
    "آدرس فید را در پیام بعدی بفرستید.\n"
    "برای انصراف: /cancel\n"
    "این درخواست تا ۵ دقیقه معتبر است."
)

ADD_FEED_INVALID = "آدرس feed نامعتبر است. لطفاً یک URL معتبر (http/https) ارائه دهید."

ADD_FEED_SUCCESS = "فید با موفقیت اضافه شد.\nآدرس: {url}\nعنوان: {title}"

ADD_FEED_EMPTY = (
    "فید با موفقیت اضافه شد، اما در حال حاضر هیچ خبری ندارد.\n"
    "آدرس: {url}\nعنوان: {title}\n"
    "اخبار جدید در pollهای بعدی دریافت می‌شوند."
)

ADD_FEED_DUPLICATE = "این فید قبلاً برای شما اضافه شده است."

ADD_FEED_UNREACHABLE = (
    "آدرس فید در دسترس نیست یا به آن پاسخ نداد.\n"
    "آدرس و اتصال شبکه را بررسی کنید و دوباره تلاش کنید."
)

ADD_FEED_NOT_A_FEED = (
    "آدرس داده‌شده یک فید RSS/Atom معتبر نیست.\n"
    "مطمئن شوید آدرس به یک فید خبری اشاره می‌کند، نه به صفحه اصلی سایت."
)

ADD_FEED_BLOCKED = "این آدرس مجاز نیست."

LIST_FEEDS_EMPTY = (
    "هیچ فیدی ثبت نشده است. از /addfeed برای اضافه کردن فید استفاده کنید."
)

LIST_FEEDS_HEADER = "فیدهای شما:\n\n"
# Telegram rejects messages longer than 4096 characters; leave room for safety.
LIST_FEEDS_MAX_MESSAGE_LENGTH = 4000

LIST_FEED_ITEM = "{id}. {title} — {url} ({status})\n"

# Appended to the status of a feed that is currently failing, so a stalled feed
# is visible in /listfeeds instead of looking healthy.
FEED_ERROR_LABELS = {
    "unreachable": "در دسترس نیست",
    "not_a_feed": "فید معتبر نیست",
    "processing": "خطای پردازش",
}
LIST_FEED_FAILING_STATUS = "فعال · خطا: {reason}"

DEL_FEED_USAGE = "لطفاً شناسه یا آدرس فید را ارائه دهید:\n/delfeed <id|url>"

DEL_FEED_PROMPT = (
    f"{DEL_FEED_USAGE}\n\n"
    "شناسه (UUID) یا آدرس فید را در پیام بعدی بفرستید.\n"
    "برای انصراف: /cancel\n"
    "این درخواست تا ۵ دقیقه معتبر است."
)

DEL_FEED_SUCCESS = "فید با شناسه {id} حذف شد."

DEL_FEED_SUCCESS_URL = "فید با آدرس {url} حذف شد."

DEL_FEED_NOT_FOUND = "فیدی با شناسه {id} یافت نشد یا دسترسی به آن ندارید."

# A pending /addfeed or /delfeed interaction expires so an old prompt cannot
# swallow a later, unrelated message.
PENDING_EXPIRED = (
    "درخواست قبلی منقضی شد. لطفاً دستور را دوباره اجرا کنید.\n/addfeed یا /delfeed"
)

CANCEL_DONE = "درخواست نیمه‌تمام لغو شد."
CANCEL_NOTHING = "درخواست نیمه‌تمامی برای لغو وجود ندارد."

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
    "دستور /delivery بدون آرگومان وضعیت ارسال خودکار را تغییر می‌دهد.\n"
    "هر بار اجرا، بین روشن و خاموش جابه‌جا می‌شود و بلافاصله ذخیره می‌شود.\n"
    "برای دیدن وضعیت فعلی، همین دستور را دوباره اجرا کنید.\n\n"
    "شکل درست: /delivery"
)

DELIVERY_ON = "ارسال خودکار خبرها روشن شد. ✅"
DELIVERY_OFF = "ارسال خودکار خبرها خاموش شد. ⛔"

# Retained for callers that render the current state without changing it.
DELIVERY_STATUS_ON = "وضعیت ارسال خودکار: روشن ✅"
DELIVERY_STATUS_OFF = "وضعیت ارسال خودکار: خاموش ⛔"

SCHEDULE_USAGE = (
    "بازه ارسال خودکار را با یکی از این مقادیر تنظیم کنید:\n"
    "/schedule <بازه>\n\n"
    f"بازه‌های مجاز: {SCHEDULE_OPTIONS}\n"
    "مقدار 0 یعنی بدون محدودیت (فوری).\n\n"
    "مثال: /schedule 6h"
)

SCHEDULE_STATUS = (
    "بازه فعلی ارسال خودکار: {interval}\n\n"
    "برای تغییر بازه: /schedule <بازه>\n"
    f"بازه‌های مجاز: {SCHEDULE_OPTIONS}\n"
    "مثال: /schedule 6h\n\n"
    "این بازه فقط روی ارسال خودکار اثر دارد؛ /news و /ask همیشه در دسترس‌اند.\n"
    "با /delivery (بدون آرگومان) می‌توانید ارسال خودکار را کاملاً خاموش کنید."
)

SCHEDULE_UPDATED = (
    "بازه ارسال خودکار به «{interval}» تغییر کرد.\n"
    "این تنظیم ذخیره شد و پس از restart هم باقی می‌ماند.\n\n"
    "یادآوری: /delivery بدون آرگومان ارسال خودکار را روشن/خاموش می‌کند."
)

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
