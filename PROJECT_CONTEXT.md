# PROJECT_CONTEXT.md
# سند مرجع پروژه هفته سوم کارورزی — AI RSS News Assistant Bot

**آخرین به‌روزرسانی:** ۱۹ اکتبر ۲۰۲۶  
**وضعیت:** زیرساخت دیتابیس و migration آماده و آزمایش شده؛ مرحله بعد، audit لایه Repository / Data Access است.  
**زبان:** Python 3.12  
**هدف:** تحویل یک دموی قابل ارائه در سریع‌ترین زمان معقول، بدون قربانی‌کردن امنیت، صحت و تست‌پذیری.

---

## ۱. دستور شروع برای چت جدید

اگر این فایل را در چت جدید دریافت کردی:

1. کل سند را بخوان و بدون درخواست توضیح دوباره از بخش «وضعیت فعلی و قدم بعدی» ادامه بده.
2. این پروژه را با پروژه قبلی ربات خبرهای ورزشی یا پروژه Text Explorer Chatbot اشتباه نگیر.
3. وضعیت واقعی فایل‌ها و Git را بررسی کن؛ branch، commit، فایل و نتیجه تست را حدس نزن.
4. فارسی ساده، دوستانه و مستقیم صحبت کن. اصطلاحات جدید را کوتاه و کاربردی توضیح بده.
5. سرعت کار مهم است: از توضیحات طولانی و تکراری پرهیز کن؛ توضیح کوتاه بده و سریع به اقدام عملی برس.
6. تغییرات را به مراحل منطقی، کوچک و قابل بازبینی تقسیم کن.
7. برای OpenCode، prompt مشخص با هدف، محدوده، ممنوعیت‌ها و تست‌های مورد انتظار آماده کن.
8. OpenCode باید commitهای Conventional Commit معنادار را خودش بسازد، وقتی یک مرحله منطقی و تست‌شده کامل شده است. نه برای هر ویرایش جزئی و نه یک commit بزرگ برای کارهای نامرتبط.
9. بدون اجازه صریح، push، merge، branch switch یا عملیات مخرب Git انجام نده.
10. پس از هر milestone مهم، همین سند را به‌روز کن و متن آماده جایگزینی یا افزوده‌شدن را ارائه بده.
11. از سؤال‌های غیرضروری اجتناب کن. اگر ابهامی با بررسی read-only حل می‌شود، ابتدا آن بررسی را انجام بده.
12. اگر فایل نیازمندی‌های اصلی شرکت در دسترس است، برای جزئیات از آن استفاده کن؛ در غیر این صورت از اطلاعات این سند استفاده کن و فقط سؤال ضروری بپرس.

---

## ۲. معرفی و هدف پروژه

پروژه یک Telegram Bot برای مدیریت RSS feedهای کاربران است.

قابلیت‌های هدف:
- افزودن، مشاهده و حذف RSS feedهای متعلق به هر کاربر.
- دریافت دوره‌ای خبرهای جدید و ذخیره پایدار آن‌ها.
- جلوگیری از ذخیره و ارسال خبرهای تکراری.
- تولید خلاصه با مدل زبانی و ارسال خودکار طبق تنظیم کاربر.
- ذخیره وضعیت روشن/خاموش بودن ارسال خودکار در دیتابیس و حفظ آن پس از restart.
- جلوگیری از فراخوانی غیرضروری LLM برای ارسال خودکار وقتی کاربر آن را خاموش کرده است.
- پاسخ به سؤال کاربر فقط با اتکا به خبرهای ذخیره‌شده و مجاز همان کاربر.
- عدم استفاده از جست‌وجوی مستقیم اینترنت برای تکمیل پاسخ Agent.
- ارائه عنوان خبر و لینک منبع واقعی، در صورت وجود شواهد کافی.
- اعلام صریح نبود اطلاعات کافی در صورت نبود شواهد.
- مدیریت خطا به‌گونه‌ای که خطای یک feed یا یک کاربر کل سامانه را متوقف نکند.
- اجرا و توسعه با Docker Compose و مستندات قابل تکرار.

این پروژه از پروژه‌های قبلی مستقل است و نباید تنظیمات یا کد آن‌ها بدون بررسی به این پروژه منتقل شود.

---

## ۳. فناوری‌ها و تصمیم‌های معماری ثبت‌شده

### فناوری‌های موجود
- Python 3.12
- `uv`
- PostgreSQL 16
- SQLAlchemy با API ناهمگام
- `asyncpg`
- Alembic برای migrationهای دیتابیس
- Docker و Docker Compose
- Telegram Bot API

ارائه‌دهنده مدل زبانی، مدل نهایی، کتابخانه RSS و جزئیات framework Agent باید از روی کد و نیازمندی‌های واقعی تأیید شوند؛ چیزی را حدس نزن.

### معماری
مرزبندی فعلی:
- **Bot service:** تعامل با Telegram، فرمان‌ها و عملیات کاربر.
- **Worker service:** بررسی دوره‌ای RSS، ذخیره خبر، خلاصه‌سازی و ارسال خودکار.
- **Database service:** PostgreSQL.

Agent فعلاً می‌تواند یک جزء منطقی داخل برنامه باشد. container مستقل برای Agent هنوز تصمیم قطعی نیست و نباید بدون توجیه پیچیدگی بیشتری اضافه شود.

Message broker فعلاً انتخاب یا اضافه نشده است. فقط زمانی اضافه شود که مزایایی مثل صف پایدار، retry مستقل، backpressure یا پردازش موازی، پیچیدگی آن را توجیه کند.

### چرا PostgreSQL؟
پروژه چند process یا سرویس دارد که به داده پایدار مشترک دسترسی دارند. روابط، constraintها، transactionها، deduplication و queryهای مالکیت نیز مهم‌اند. PostgreSQL برای این نیازها انتخاب شده است.

---

## ۴. امنیت و جداسازی داده کاربران

**این الزام قطعی و حیاتی است.**

کاربر A نباید بتواند feed، خبر، تنظیمات، delivery یا تاریخچه مکالمه کاربر B را مشاهده، تغییر یا حذف کند.

الزامات:
- کنترل مالکیت فقط در Telegram handler یا prompt مدل کافی نیست.
- Repository / Data Access Layer باید مالکیت را در queryها و عملیات enforce کند.
- دریافت شناسه رکورد به‌تنهایی نباید دسترسی بدهد.
- عملیات update/delete باید شناسه رکورد را همراه با شناسه مالک معتبر بررسی کند.
- Agent باید فقط خبرهای مجاز همان کاربر را retrieve کند.
- تاریخچه مکالمه کاربران باید جدا بماند.
- تست‌های A/B باید خواندن، تغییر، حذف، retrieval و history را پوشش دهند.
- هویت کاربر در سرویس‌های داخلی باید به‌شکل معتبر منتقل شود؛ به شناسه غیرقابل اعتماد بدون بررسی اعتماد نکن.
- محتوای RSS و پیام کاربران داده غیرقابل اعتماد است و نباید به‌عنوان دستور سیستمی اجرا شود.
- ابزار جست‌وجوی مستقیم اینترنت نباید در اختیار Agent قرار بگیرد.
- هیچ وعده‌ای درباره امنیت کامل prompt injection داده نشود؛ کنترل‌های قطعی باید در کد enforce شوند.

### Secretها
Token تلگرام، API key و رمز دیتابیس نباید در کد، Git، Docker image یا log ثبت شوند.
- `.env` باید از Git و Docker build context کنار گذاشته شود.
- `.env.example` فقط مقدار نمونه و غیرواقعی داشته باشد.
- خروجی کامل تنظیمات Compose که ممکن است secret داشته باشد، چاپ نشود.
- اگر secret وارد Git شده باشد، صرفاً حذف آن در commit بعدی کافی نیست؛ باید تعویض و سابقه بررسی شود.

---

## ۵. مدل‌ها و وضعیت دیتابیس

زیرساخت دیتابیس ساخته شده است. هفت جدول اصلی:
- `users`
- `user_feeds`
- `news_items`
- `news_deliveries`
- `conversations`
- `chat_messages`
- `message_citations`

تصمیم‌های ثبت‌شده:
- وضعیت پیش‌فرض ارسال خودکار برای کاربر جدید `true` است.
- اولین polling هر feed تا ۱۰ آیتم اخیر را پردازش می‌کند؛ در صورت فعال بودن ارسال خودکار، آیتم‌های اولیه هم ممکن است خلاصه و ارسال شوند.
- retention خبرها ۳۰ روز است.
- حذف feed باید خبرهای متعلق به همان feed را حذف کند.
- هر news item به یک user و یک feed تعلق دارد؛ association table بین feed و news در طراحی فعلی وجود ندارد.

### URL normalization و deduplication
ماژول `src/ai_rss_news_assistant/db/url_normalization.py` شامل `normalize_url()` است.

منطق گزارش‌شده:
- scheme و hostname به حروف کوچک تبدیل می‌شوند.
- پورت پیش‌فرض حذف می‌شود.
- fragment حذف می‌شود.
- پارامترهای شناخته‌شده tracking مانند `utm_*`, `fbclid`, `gclid`, `ref`, `_ga`, `_gl`, `mc_cid`, `mc_eid` حذف می‌شوند.
- سایر query parameterها حفظ می‌شوند.

Unique constraint مربوط به dedup برابر `(user_id, normalized_url)` است؛ بنابراین یک کاربر نمی‌تواند همان URL نرمال‌شده را چند بار ثبت کند، اما کاربران مختلف می‌توانند همان URL را داشته باشند.

**ریسک شناخته‌شده:** migration دوم ممکن است هنگام backfill با URLهای تکراری موجود برای یک کاربر به unique constraint برخورد کند. این حالت روی دیتابیس تازه آزمایش نشده است؛ قبل از اعمال migration روی دیتابیس دارای داده واقعی باید duplicateهای احتمالی بررسی شوند.

---

## ۶. Migration و Docker

Migrationهای موجود:
- `0001_initial_schema`
- `0002_add_normalized_url`

Migrationها خودکار در startup سرویس Bot یا Worker اجرا نمی‌شوند؛ اجرای آن‌ها مرحله‌ای صریح است.

### تغییرات ثبت‌شده
- `Dockerfile` فایل `alembic.ini` و پوشه `alembic/` را در image کپی می‌کند.
- `.dockerignore` اضافه شده و `.env`، `.venv/`، cacheها، `.git/` و فایل‌های محلی غیرضروری را از build context کنار می‌گذارد.
- سرویس one-off با نام `migrate` در `compose.yaml` وجود دارد.
- این سرویس از همان Dockerfile استفاده می‌کند، منتظر healthy شدن `db` می‌ماند، `DATABASE_URL` می‌گیرد و `alembic upgrade head` را اجرا می‌کند.
- Migration به‌صورت خودکار در startup Bot/Worker اجرا نمی‌شود.
- README workflow مربوط به migration را مستند می‌کند.

فرمان‌های ثبت‌شده:

```bash
# Apply migrations
docker compose up migrate

# Alternative one-off invocation
docker compose run --rm migrate

# Inspect current revision
docker compose run --rm migrate current
```

### Validation گزارش‌شده
طبق گزارش OpenCode:
- `uv run pytest -q`: 42 passed.
- Ruff check موفق.
- Ruff format موفق؛ 17 فایل از قبل format شده بودند.
- Docker image با موفقیت build شد و فایل‌های Alembic در `/app/` تأیید شدند.
- هر دو migration روی PostgreSQL 16 دورریختنی اجرا شدند.
- `alembic current` نسخه `0002_add_normalized_url (head)` را تأیید کرد.
- منابع آزمایشی پاک شدند و volume اصلی `db_data` استفاده یا تغییر داده نشد.

این نتایج از گزارش OpenCode آمده‌اند. بی‌دلیل تست‌های پرهزینه را تکرار نکن؛ در صورت نیاز به بررسی مجدد، دلیل و محدوده را مشخص کن.

---

## ۷. Git و commitهای شناخته‌شده

آخرین وضعیت گزارش‌شده:
- Branch: `feat/database-layer`
- Working tree: clean
- آخرین commit: `ecc9919 docs: document alembic migration workflow`
- هیچ push یا merge انجام نشده است.

سه commit مربوط به اصلاح migration:
- `26abde7 fix: include alembic in image and add .dockerignore for build context`
- `93a4ce4 feat: add alembic migrate service for explicit schema updates`
- `ecc9919 docs: document alembic migration workflow`

Commitهای قبلی:
- `c55d71e fix: enforce per-user cross-feed news deduplication`
- `882fde6 fmt: apply ruff formatting to db module`
- `56f0898 test: add database isolation and cascade delete tests`
- `f037bfe dep: add aiosqlite dev dependency for async SQLite testing`
- `c292dd0 fix: remove redundant index=True declarations from model columns`
- `3052489 fix: ignore extra env vars in Settings; skip local .env in tests`
- `b490341 feat: add Alembic initial migration for database schema`
- `faf7906 feat: add SQLAlchemy domain models with per-user ownership and cascade deletes`
- `724a723 feat: add database session management with async SQLAlchemy engine`
- `073e102 chore: add SQLAlchemy, asyncpg, and Alembic dependencies`

قبل از هر کار جدید، وضعیت واقعی را بررسی کن:

```bash
git status --short --branch
git log --oneline -5
```

این وضعیت ممکن است تغییر کرده باشد؛ آن را قطعی فرض نکن. Push، merge، branch switch یا عملیات مخرب Git بدون اجازه صریح ممنوع است.

---

## ۸. کارهای باقی‌مانده

Repository / Data Access Layer مراحل زیر در بخش `feat/repository-layer` پیاده‌سازی و تست شده‌اند:
- `users`: get/create/update/deactivate (commit `aea98f3`)
- `user_feeds`: create/get/list/update/delete با مالکیت در query (commit `aea98f3`)
- `news_items`: create/get/list با مالکیت در query و تکرار نشدن `normalized_url` برای هر کاربر (commit فعلی)
- `news_deliveries`: create/get/list با مالکیت در query و تکرار نشدن `(user_id, news_item_id)` (commit فعلی)

کارهای اصلی محصول:
- Telegram handlers و فرمان‌های مدیریت feed
- RSS fetching/parsing و scheduling
- LLM summarization
- delivery state و ارسال مقاوم در برابر retry
- Agent retrieval و پاسخ grounded همراه با citation
- مدیریت خطا، retry و logging
- integration tests و اجرای end-to-end
- تکمیل README و مستندسازی تصمیم‌های معماری

موارد فنی مطرح‌شده در audit قبلی که فعلاً فقط باید در صورت ارتباط بررسی شوند:
- احتمال تفاوت نام برخی composite foreign keyها بین مدل‌ها و migrationها و در نتیجه احتمال Alembic autogenerate drift.
- `updated_at` با `onupdate=func.now()` در ORM الزاماً trigger سطح دیتابیس ایجاد نمی‌کند.
- احتمال index اضافی روی `users.telegram_user_id`، چون `unique=True` معمولاً خودش unique index ایجاد می‌کند.
- رفتار cascade مربوط به citationها هنگام حذف feed/news باید در تست‌ها صریحاً بررسی شود.

در مرحله فعلی، scope را بی‌دلیل گسترش نده و schema یا migration را تغییر نده، مگر blocker واقعی پیدا شود؛ در آن صورت ابتدا گزارش بده.

طبق بررسی قبلی، Repository / Data Access Layer هنوز پیاده‌سازی نشده بود؛ این موضوع باید با audit read-only جدید تأیید شود.

کارهای اصلی محصول:
- Repositoryها و عملیات امن per-user
- Telegram handlers و فرمان‌های مدیریت feed
- RSS fetching/parsing و scheduling
- deduplication و ذخیره خبر
- LLM summarization
- delivery state و ارسال مقاوم در برابر retry
- Agent retrieval و پاسخ grounded همراه با citation
- مدیریت خطا، retry و logging
- integration tests و اجرای end-to-end
- تکمیل README و مستندسازی تصمیم‌های معماری

موارد فنی مطرح‌شده در audit قبلی که فعلاً فقط باید در صورت ارتباط بررسی شوند:
- احتمال تفاوت نام برخی composite foreign keyها بین مدل‌ها و migrationها و در نتیجه احتمال Alembic autogenerate drift.
- `updated_at` با `onupdate=func.now()` در ORM الزاماً trigger سطح دیتابیس ایجاد نمی‌کند.
- احتمال index اضافی روی `users.telegram_user_id`، چون `unique=True` معمولاً خودش unique index ایجاد می‌کند.
- رفتار cascade مربوط به citationها هنگام حذف feed/news باید در تست‌ها صریحاً بررسی شود.

در مرحله فعلی، scope را بی‌دلیل گسترش نده و schema یا migration را تغییر نده، مگر blocker واقعی پیدا شود؛ در آن صورت ابتدا گزارش بده.

---

## ۹. شیوه همکاری با OpenCode و Git

- ابتدا branch و working tree را بررسی کن.
- برای قدم بعدی، audit فقط‌خواندنی انجام بده؛ فعلاً تغییر کد نده.
- هر prompt باید هدف، محدوده فایل‌ها، محدودیت‌ها، تست‌ها و گزارش نهایی را مشخص کند.
- پس از implementation، diff و تست‌های مربوط را بررسی کن.
- OpenCode باید خودش commitهای Conventional Commit معنادار بسازد، وقتی یک واحد منطقی و تست‌شده آماده است.
- تعداد commitها متعادل باشد: نه commit برای هر تغییر جزئی، نه یک commit بزرگ برای کارهای نامرتبط.
- Push، merge، branch switch و عملیات مخرب Git بدون اجازه صریح انجام نشود.
- اگر Docker یا سرویس موردنیاز در محیط در دسترس نیست، محدودیت را گزارش کن؛ validation اجرا‌نشده را موفق اعلام نکن.

---

## ۱۰. قدم بعدی دقیق

### مرحله فعلی: Repository / Data Access Layer — بخش News + Deliveries انجام شده

Repository / Data Access Layer برای `users` و `user_feeds` (commit `aea98f3`) و برای `news_items` و `news_deliveries` (commit فعلی) پیاده‌سازی و تست شده است. هر عملیات خواندن/به‌روزرسانی/حذف مالکیت را در query enforce می‌کند.

### قدم بعدی: Repository برای `conversations`، `chat_messages` و `message_citations`

پolishing و تکمیل Repositoryهای قبلی، سپس پیاده‌سازی Repository برای `conversations`، `chat_messages` و `message_citations` با mesma الگو مالکیت در query.

### مرحله فعلی: Repository / Data Access Layer — فقط audit

از OpenCode بخواه وضعیت واقعی را بررسی کند و چیزی را تغییر ندهد. باید مشخص شود:
- آیا Repository یا data-access module وجود دارد یا نه؛
- ساختار package و ماژول‌های فعلی چیست؛
- مدل‌ها و session management چگونه پیاده‌سازی شده‌اند؛
- برای هر entity چه عملیات‌هایی لازم‌اند؛
- کدام queryها مالکیت را enforce می‌کنند؛
- تست‌های موجود کدام حالت‌های isolation و cascade را پوشش می‌دهند؛
- کوچک‌ترین مرحله منطقی برای پیاده‌سازی چیست.

پس از دریافت گزارش، آن را بررسی کن و سپس Repositoryها را مرحله‌ای همراه با تست‌های مالکیت پیاده‌سازی کن. هدف سرعت بیشتر است؛ audit را محدود و نتیجه‌محور نگه دار.

### Prompt آماده برای OpenCode

```text
Perform a strictly read-only audit of the Repository / Data Access Layer in the current AI RSS News Assistant Bot repository.

Do not modify files, create commits, switch branches, push, merge, reset, run migrations, or alter any database or persistent volume. Do not expose secrets or print the full output of Docker Compose configuration.

1. Report the current Git branch, working-tree status, and the last five commits.
2. Inspect the source tree and determine whether any repository/data-access layer already exists. Do not assume it is absent.
3. Inspect SQLAlchemy models, database session/engine setup, Alembic migrations, and current database-related tests.
4. For each entity/table, list the expected read/create/update/delete operations and whether ownership is enforced by query conditions or database constraints.
5. Trace existing ownership controls for feeds, news items, deliveries, conversations, chat messages, and citations. Distinguish implemented controls from planned controls.
6. Review existing tests for cross-user isolation. Identify missing tests for read, update, delete, retrieval, conversation history, and cascade behavior.
7. Identify relevant implementation blockers or schema/model inconsistencies, but do not fix them.
8. Recommend the smallest coherent next implementation step. Keep existing models and migrations unchanged during this audit.

Return a concise report with exact file paths, confirmed findings versus assumptions, test coverage, risks, and a proposed implementation sequence. Do not implement anything or create commits.
```

پس از audit، گزارش را بررسی کن و اولین واحد کوچک Repository را با تست‌های ownership اجرا کن. بعد از milestone، همین context را به‌روزرسانی کن.

---

## ۱۱. واژه‌نامه کوتاه

- **Repository / Data Access Layer:** بخش برنامه‌ای که خواندن و نوشتن داده را مدیریت می‌کند؛ باید عملیات را با مالکیت درست محدود کند.
- **Migration:** تغییر نسخه‌بندی‌شده ساختار دیتابیس.
- **Foreign key:** قیدی که رابطه معتبر بین رکوردها را حفظ می‌کند.
- **Unique constraint:** جلوگیری از تکرار یک مقدار یا ترکیب مشخص.
- **Isolation:** جلوگیری از دسترسی یا اثرگذاری یک کاربر روی داده کاربر دیگر.
- **Idempotency:** اجرای دوباره یک عملیات بدون اثر تکراری ناخواسته.
- **RAG / Retrieval:** پیدا کردن داده مرتبط و استفاده از آن برای پاسخ مستند.
- **Prompt injection:** محتوای غیرقابل اعتمادی که تلاش می‌کند رفتار مدل را از قواعد اصلی منحرف کند.
- **Health check:** بررسی خودکار سلامت یا آمادگی سرویس.
- **Trade-off:** بده‌بستان میان مزایا، هزینه‌ها و محدودیت‌های یک انتخاب.

---

**پایان سند مرجع پروژه**

این سند برای انتقال سریع context است، نه جایگزین repository واقعی، نیازمندی‌های شرکت یا نتایج تازه تست‌ها. وضعیت Git، فایل‌ها و تست‌ها را پیش از هر تغییر جدید تأیید کن.
