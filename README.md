# telegram-media-grabber

بوت **Telegram** لتنزيل الفيديو والصوت والصور والألبومات والملفات من أكبر عدد ممكن من
المنصات. الواجهة الوحيدة هي تيليجرام: لا موقع، ولا لوحة تحكم.

المسار: أرسل رابطا → يحلل البوت الرابط → يعرض المنصة والعنوان والصورة المصغرة → يضع زر
حقيقيا لكل جودة موجودة فعلا في المصدر مع حجمها المتوقع → ينزل مع نسبة تقدم → يرسل الملف.

```
┌─ المستخدم ───────────┐        ┌──────────────── media_bot ────────────────┐
│ رابط / وسائط محولة    │──tg──▶ │ handlers → platform_detector → registry   │
│ ضغط زر الجودة         │        │                    │                      │
│ نتيجة التقدم ◀──────┴────────┼─ senders ← queue ←─ extractors            │
└─────────────────────┘          │  (bot_api /        (yt-dlp · gallery-dl ·  │
                                 │   local_api /      direct · telegram)      │
                                 │   mtproto / split) ─ processing (FFmpeg)   │
                                 └ storage · db · auth · security ────────────┘
```

---

## التثبيت

المتطلبات: Python 3.11 أو أحدث (طوّرت واختبرت على 3.13)، و FFmpeg على PATH، و توكن بوت من
[@BotFather](https://t.me/BotFather). بدون FFmpeg يعمل البوت، لكن الدمج وتحويل الصيغ وتقسيم
الملفات الكبيرة تتعطل وتعلن نفسها في `doctor` بدلا من الفشل بصمت. `aria2c` اختياري لتنزيل
متعدد الأجزاء أسرع.

```bash
git clone https://github.com/Steve6546/telegram-media-grabber.git
cd telegram-media-grabber

python3 -m venv .venv && source .venv/bin/activate      # Windows: scripts\setup.ps1
pip install -e ".[dev]"
cp .env.example .env                                     # ثم عدّله
```

أهم ما في `.env`:

```ini
BOT_TOKEN=123456:your-token-here
ALLOWED_USER_IDS=your_telegram_user_id     # فارغ يعني أي شخص يستطيع الاستخدام
COOKIE_ENC_KEY=                            # ولّده بأمر: grabber keygen
```

افحص البيئة ثم شغّل:

```bash
grabber doctor --check-token
grabber run
```

### Docker مع دعم الملفات الكبيرة

```bash
cp .env.example .env    # ضع BOT_TOKEN و TG_API_ID و TG_API_HASH
docker compose up -d --build
```

يشغّل `docker-compose.yml` خادم [Local Bot API](https://hub.docker.com/r/aiogram/telegram-bot-api)
بجانب البوت على نفس مجلد `/data`، فيرتفع سقف الإرسال من 50 ميغابايت إلى 2 غيغابايت.

---

## أوامر تيليجرام

| الأمر | الوظيفة |
|---|---|
| إرسال رابط | تحليل ثم صورة مصغرة ثم أزرار الجودات الحقيقية |
| `/start` | القائمة الرئيسية |
| `/help` | الشرح وسقوف المسارات |
| `/status` | حالة المحركات والعتاد الفعلي: ffmpeg، aria2c، التشفير، الطابور |
| `/tasks` | المهام الجارية بنسب تقدمها |
| `/cancel` | إلغاء مهمة، أو كل مهامك |
| `/cookies` | رفع ملف cookies بصيغة Netscape للمحتوى الخاص بك |
| `/cookies_clear` | حذف الكوكيز المخزنة |
| إعادة توجيه وسائط إلى البوت | يعيد إرسالها إليك (حد `getFile` هو 20 ميغابايت) |

---

## المحركات وتغطية المنصات

لا قائمة ثابتة مكتوبة يدويا في الكود. التغطية هي ما يوفّره المحركان المركّبان فعلا:

| المحرك | ما قسته على الإصدار المركّب | التخصص |
|---|---|---|
| yt-dlp | 1863 مستخرجا | فيديو وصوت وبث وقوائم تشغيل على أكثر من 1800 موقع |
| gallery-dl | 233 فئة ومئات المستخرجات | صور وألبومات: Pinterest و Instagram و X و Tumblr و Flickr و DeviantArt و Unsplash و ArtStation و Weibo و Imgur و Reddit وغيرها |
| direct | أي رابط ملف مباشر | صورة أو فيديو أو صوت أو ملف بلا تحليل صفحة، مع استئناف عبر Range |
| telegram | وسائط محادثاتك | ما تملك أنت صلاحية الوصول إليه داخل تيليجرام |

جدول التوجيه في `media_bot/platform_detector.py` يسمّي 32 منصة ويحدّد أي محرك يجرَّب أولا،
وأي رابط غير مذكور يمرّ لمسار generic بدلا من أن يرفض.

### ما تحققت منه على الشبكة في 2026-11-05

```
[OK] youtube-cc         engine=ytdlp    28.5 MB mp4 downloaded
[OK] direct-mp4         engine=direct    2.8 MB mp4 downloaded
[OK] direct-jpg         engine=direct    518 KB - 2 MB jpg (picsum 2000x1200)
[OK] commons-hires-jpg  engine=direct   53.9 MB jpg, 8032x5330
```

أعد نفس الدليل:

```bash
python scripts/live_probe.py --download
grabber analyze "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
```

### القيود، بلا تجميل

| المنصة | الحالة | السبب |
|---|---|---|
| YouTube | يعمل. جودة 360p فقط بدون دخول | يوتيوب يطلب تأكيد هوية من عنوان الخادم. مع `/cookies` تظهر الجودات الأعلى |
| Vimeo و Instagram و Facebook و X و Reddit (بعضها) | تحتاج كوكيز | هذه المواقع تشترط جلسة الآن، ورسالة البوت تقول ذلك صراحة |
| Snapchat | Spotlight فقط | مستخرج yt-dlp اسمه `SnapchatSpotlight`، واللقطات الخاصة لا سبيل إليها |
| WhatsApp | غير مدعوم | لا توجد واجهة رسمية تتيح لطرف ثالث تنزيل وسائط محادثاتك، وجدول التوجيه يصرّح بذلك |
| محتوى DRM | يرفض عمدا | إن بلغ المحرك `has_drm` يرفع `DRMMediaError` ولا يبحث عن محرك آخر ليدور عليه |
| ملف أكبر من 50 ميغابايت | يحتاج مسارا بديلا | `local_api` أو `mtproto` أو تقسيم بالـ FFmpeg، وإلا رسالة تشرح السقف |

---

## اختيار الجودة

`media_bot/quality.py` يبني الخيارات من جدول الصيغ الذي أرجعه المحرك، لا من قائمة جاهزة:

- سطرا لكل ارتفاع موجود فعلا من 144p إلى 8K، مع حجمه الحقيقي أو المقدّر من الـ bitrate والمدة.
- الصوت منفصلا عن الفيديو؟ يوضع المعرفان في مواصفة واحدة ويُدمجان بـ FFmpeg، ويظهر «يُدمج
  الصوت تلقائيا» في الزر والكابشن.
- «أعلى جودة متاحة» تترك القرار للمحرك عبر `bv*+ba/b`.
- «فيديو بدون صوت» و«صوت فقط» (m4a و opus و mp3) سطوران مستقلان.
- لا يوضع زر لجودة غير موجودة: غاب 480p من المصدر غاب زرّه. ولا مصدر بلا صوت فيه، فلا زرّ
  مكرر لـ«بدون صوت».
- ffprobe يتحقق من الارتفاع الذي وصل فعلا، ويظهر في الكابشن إن اختلف عمّا طُلب.

---

## الملفات الكبيرة: القرار التقني

| المسار | السقف | شروطه |
|---|---|---|
| Bot API الافتراضي | 50 MB إرسال، و 20 MB تنزيل | لا شيء، لكنه يحتاج بديلا للكبير |
| Local Bot API Server | 2 GB | خادم إضافي مع API_ID و API_HASH، ونفس نظام الملفات مع البوت |
| MTProto عبر Telethon | 2 GB، و 4 GB مع Premium | الملف يظهر مرسلا من الحساب لا من البوت، والحساب يجب أن يعرف المستلم |
| التقسيم بالـ FFmpeg | بلا سقف عملي | `-c copy` بلا إعادة ترميز: تصل أجزاء، لكنها ليست ملفا واحدا |

يختار البوت بينها بترتيب تفضيل مبني على اختبار فعلي عند الإقلاع (`probe_senders`)، ولا يعدّ
خادم Local API جاهزا لمجرد أن رابطه مضبوط. والمسار الذي استُخدم يُذكر في رسالة الإرسال.

---

## البنية

إضافة منصة جديدة تعني تعديل محرك واحد، لا إعادة كتابة البوت:

```
media_bot/
  config.py              Configuration: env ثم YAML ثم قيم افتراضية
  security.py            SSRF guard، تطبيع الروابط، حجز أسماء الملفات
  errors.py              Error handler: أخطاء مصنّفة برسائل توضح ما يجري
  platform_detector.py   Platform detector وتوجيه المحركات
  quality.py             Quality selector: الجودات الموجودة فعلا فقط
  extractors/
    base.py              عقود MediaExtractor: Bundle و Variant و MediaItem
    ytdlp_engine.py      Downloader engine للفيديو والصوت والبث وقوائم التشغيل
    gallerydl_engine.py  محرك الصور والألبومات عبر واجهة سطر الأوامر الثابتة
    direct_url.py        محرك الملفات المباشرة مع استئناف Range
    registry.py          التوجيه وترتيب الأخطاء، و DRM يوقف البحث
  processing/
    ffmpeg.py            Video/Audio processor: remux، to_audio، trim، shrink
    probe.py             تحقق ffprobe مما وصل فعلا
  bigfiles.py            تقسيم ما يكبر عن السقف
  queue.py               Download Queue: إشارات إيقاف عامة ولكل مستخدم، إلغاء، تقدم
  storage.py             Storage Manager: مجلد لكل مهمة، مخزن مؤقت، تنظيف ب TTL
  db.py                  SQLite: jobs و dedupe و bundles و prefs و cookie meta
  auth.py                Authentication Manager: تحليل Netscape وخزنة Fernet
  telegram_media.py      وسائط تيليجرام التي تملك صلاحيتها
  senders.py             Bot API و Local API مع سقوفهما الحقيقية
  mtproto.py             مسار MTProto الاختياري
  service.py             التنسيق: تحليل ثم تنزيل ثم تحقق ثم تسليم
  app.py، cli.py، doctor.py
```

---

## الاختبارات

```bash
python -m pytest -q              # 258 اختبارا بلا شبكة
python -m pytest -q -m live      # الفحص الحي على مواقع عامة
```

تغطي: حواجز SSRF بعشرة أهداف، تنظيف الأسماء، تصنيف أخطاء المحركات، اختيار الجودة من جدول
صيغ واقعي (عدم اختلاق جودات، حساب الأحجام، علامة الدمج، منع الأزرار المكررة)، محرك الروابط
المباشرة على خادم HTTP محلي بما فيه التحويل والاستئناف ورفض 403، محرك الصور بمخرجات JSON
مسجلة، الطابور بالتزامن والإلغاء والفشل والعدالة لكل مستخدم، سقوف الإرسال واختيار الوسيلة،
وتدفق كامل من تحليل إلى تسليم إلى حذف، وخزنة الكوكيز بمقارنة ملف كتبه `http.cookiejar`
الحقيقي، و FFmpeg فعليا على مقطع مولّد: remux، استخراج mp3، قص، تصغير، تقسيم، وتحقق ffprobe.

---

## الأمان

- **SSRF**: كل رابط وكل تحولة فيه يمرّان على `security.check_url`: مخطط http أو https فقط،
  بلا بيانات دخول ضمّنها المستخدم، مع تحليل DNS لكل سجلات A و AAAA ورفض العناوين الخاصة
  (loopback و RFC1918 و ULA و link-local و CGNAT و multicast و reserved) ونطاق metadata
  و `host.docker.internal`. لا زر إيقاف لهذا البوابة.
  **بقية معروفة ومعلنة**: التحقق يحدث قبل تسليم الرابط للمحرك، والطلب الفعلي يحل DNS مرة
  ثانية، فتبقى ثغرة DNS-rebinding ذات TTL صفر. سدها يحتاج resolver أو وكيلا أمام المحركات،
  وهذا المشروع لا يوفّره.
- **لا تنفيذ لأوامر من المستخدم**: لا `eval` ولا تمرير نص المستخدم إلى صدفة. الروابط تذهب
  إلى مطاعم خيارات مهيكلة فقط، و`callback_data` مقصور على صيغ معروفة.
- **الأسماء**: كل اسم ملف ينظف من `../` ومن أسماء الملفات الخطرة في Windows (`CON`، `nul`،
  `..`) ويقص عند 120 حرفا، ويكتب داخل مجلد المهمة وحده.
- **الكوكيز**: تتحقق من صيغة Netscape بسبعة حقول بالترتيب الرسمي، وتخزن بمفتاح Fernet إن ضبطت
  `COOKIE_ENC_KEY`، بصلاحية 600؛ وتُمسح النسخة النصية القديمة تلقائيا عند تفعيل التشفير؛
  وتُكتب نصا في مجلد المهمة أثناء التنزيل فقط ثم تحذف في `finally`؛ ولا تظهر في أي رد.
- **السجل**: `RedactingFormatter` يحجب قيم token و key و secret و password قبل الكتابة.
- **الحدود**: `ALLOWED_USER_IDS`، `MAX_DOWNLOADS_PER_HOUR`، `MAX_CONCURRENT`،
  `PER_USER_CONCURRENT`، `MAX_FILE_MB`.

### إن كنت قادما من مستودعاتك القديمة

ملفات `config.json` و `setup.py` و `config.py` في مستودعات `telegram` و `Telegram1999` و
`Telegram_BOT` و `1` تحتوي توكنات بوت حقيقية منشورة في مستودعات عامة. دوّرها من @BotFather
بأمر `/revoke` قبل استخدام هذا المشروع، ولا تنسخ تلك الملفات إلى هنا.

---

## استكشاف الأخطاء

| ما تراه | ما يعنيه وما تعمل |
|---|---|
| `doctor` يقول FFmpeg مفقود | ثبّته. بدونها الدمج والتحويل والتقسيم معطلة صراحة |
| «هذا المحتوى خاص ويحتاج إلى إثبات هوية» | الموقع يطلب جلسة. استخدم `/cookies` |
| جودة واحدة ظهرت | هذا ما أعطاه المحرك بلا دخول. مع كوكيز تظهر الأعلى |
| «الملف أكبر من سقف Bot API» | فعل `local_api` أو `mtproto`؛ ومع FFmpeg سيقسم البوت الملف تلقائيا |
| «انتهت صلاحية هذه القائمة» | رمز القائمة معمّر `DEDUPE_TTL_MINUTES` أي 60 دقيقة. أرسل الرابط من جديد |
| البوت لا يرد | `grabber doctor --check-token` ثم `/tasks` لترى إن كان الطابور ممتلئا |

---

## الترخصة

MIT. يعتمد على yt-dlp و gallery-dl و FFmpeg و aiogram، كل منها برخصتها. أنت مسؤول عن شروط
استخدام كل منصة وعن حقوق ما تنزله. هذا المشروع لا يتجاوز DRM ولا يزيّف صلاحيات حسابات الآخرين.

Sources:
- [aiogram/telegram-bot-api على Docker Hub](https://hub.docker.com/r/aiogram/telegram-bot-api)
- [Self-hosting the Telegram Bot API to bypass the 50 MB upload limit](https://dev.to/joybtw/how-i-self-hosted-the-telegram-bot-api-with-docker-to-bypass-50mb-upload-limits-483a)
- [Local Telegram Bot API Server in Docker: 2 GB file limit](https://bigmike.help/en/devops/local-telegram-bot-api-advantages-limitations-of-the-standard-api-and-set-eb4a3b/)
