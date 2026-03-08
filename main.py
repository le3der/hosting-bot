import os
import subprocess
import psutil
import asyncio
import sqlite3
import time
import logging
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ─── إعداد اللوجر ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("panel.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── إعدادات عامة ────────────────────────────────────────────────
TOKEN    = os.getenv("BOT_TOKEN")
ADMIN    = int(os.getenv("ADMIN_ID", "0"))
DB_FILE  = "panel.db"
BOTS_DIR = "bots"
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/your_channel")
BOT_URL     = os.getenv("BOT_URL",     "https://t.me/your_bot")

# ─── الباقات ──────────────────────────────────────────────────────
PLANS = {
    "free":  {"bots": 1,  "size_mb": 10,   "label": "مجاني",    "price": 0},
    "basic": {"bots": 5,  "size_mb": 100,  "label": "أساسية",   "price": 5},
    "pro":   {"bots": 20, "size_mb": 500,  "label": "احترافية", "price": 15},
}

# ─── فحص الكود ──────────────────────────────────────────────────
def is_code_safe(code: str) -> tuple[bool, str]:
    # لا يوجد قيود — كل الكود مسموح
    return True, ""

def extract_imports(code: str) -> list[str]:
    """استخرج أسماء المكتبات من الكود"""
    stdlib = {
        "os","sys","re","io","abc","ast","csv","cgi","cmd","dis","ftp","ftplib",
        "gc","gzip","html","http","json","math","mmap","pdb","pty","pwd","queue",
        "random","shlex","shutil","signal","smtplib","socket","sqlite3","ssl",
        "stat","string","struct","subprocess","tarfile","tempfile","textwrap",
        "threading","time","traceback","typing","unittest","urllib","uuid",
        "warnings","weakref","xml","zipfile","zlib","asyncio","pathlib","enum",
        "functools","itertools","collections","contextlib","dataclasses","hashlib",
        "hmac","logging","inspect","copy","decimal","fractions","base64","binascii",
        "codecs","datetime","calendar","locale","platform","pprint","pickle",
        "shelve","dbm","readline","rlcompleter","token","tokenize","keyword",
        "builtins","importlib","pkgutil","site","sysconfig","types","operator",
    }
    # خريطة: اسم الـ import → اسم الـ pip package الصحيح
    PKG_MAP = {
        "telegram":      "python-telegram-bot>=20.0",
        "cv2":           "opencv-python",
        "PIL":           "Pillow",
        "sklearn":       "scikit-learn",
        "bs4":           "beautifulsoup4",
        "dotenv":        "python-dotenv",
        "yaml":          "PyYAML",
        "dateutil":      "python-dateutil",
        "cryptography":  "cryptography",
        "serial":        "pyserial",
        "usb":           "pyusb",
        "gi":            "PyGObject",
        "wx":            "wxPython",
        "Crypto":        "pycryptodome",
        "jwt":           "PyJWT",
        "magic":         "python-magic",
        "MySQLdb":       "mysqlclient",
        "psycopg2":      "psycopg2-binary",
        "pymongo":       "pymongo",
        "redis":         "redis",
        "celery":        "celery",
        "flask":         "Flask",
        "django":        "Django",
        "fastapi":       "fastapi",
        "uvicorn":       "uvicorn",
        "starlette":     "starlette",
        "pydantic":      "pydantic",
        "aiohttp":       "aiohttp",
        "httpx":         "httpx",
        "requests":      "requests",
        "numpy":         "numpy",
        "pandas":        "pandas",
        "matplotlib":    "matplotlib",
        "scipy":         "scipy",
        "tensorflow":    "tensorflow",
        "torch":         "torch",
        "transformers":  "transformers",
        "openai":        "openai",
        "anthropic":     "anthropic",
    }
    pkgs = set()
    for line in code.splitlines():
        line = line.strip()
        m = re.match(r"^import\s+([\w]+)", line)
        if m:
            pkgs.add(m.group(1))
        m = re.match(r"^from\s+([\w]+)", line)
        if m:
            pkgs.add(m.group(1))
    result = []
    for p in pkgs:
        if p in stdlib or p.startswith("_"):
            continue
        result.append(PKG_MAP.get(p, p))
    return result

async def install_requirements(packages: list[str], status_msg) -> str:
    """تثبيت المكتبات وإرجاع تقرير"""
    if not packages:
        return "لا توجد مكتبات خارجية للتثبيت."
    results = []
    for pkg in packages:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pip", "install", pkg, "--quiet", "--break-system-packages",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode == 0:
                results.append(f"✅ {pkg}")
            else:
                err = stderr.decode()[:100]
                results.append(f"❌ {pkg}: {err}")
        except asyncio.TimeoutError:
            results.append(f"⏰ {pkg}: تجاوز الوقت")
        except Exception as e:
            results.append(f"❌ {pkg}: {e}")
    return "\n".join(results)

async def auto_fix_imports(uid: str, fname: str, stderr_text: str, app) -> bool:
    """لو الخطأ ImportError، ثبّت المكتبة تلقائياً وأعد التشغيل"""
    # استخرج اسم المكتبة من رسالة الخطأ
    m = re.search(r"(?:No module named|cannot import name '.+' from)\s+'?([\w]+)", stderr_text)
    if not m:
        return False
    bad_module = m.group(1)
    path = f"{BOTS_DIR}/{uid}/{fname}"
    # استخدم الخريطة لمعرفة اسم الـ pip
    PKG_MAP = {
        "telegram": "python-telegram-bot>=20.0",
        "cv2": "opencv-python", "PIL": "Pillow",
        "sklearn": "scikit-learn", "bs4": "beautifulsoup4",
        "dotenv": "python-dotenv", "yaml": "PyYAML",
        "Crypto": "pycryptodome", "jwt": "PyJWT",
        "psycopg2": "psycopg2-binary", "MySQLdb": "mysqlclient",
    }
    pip_name = PKG_MAP.get(bad_module, bad_module)
    log.info(f"🔧 auto-fix: تثبيت {pip_name} بسبب ImportError في {fname}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "pip", "install", pip_name, "--quiet", "--break-system-packages",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            log.info(f"✅ تثبيت {pip_name} نجح — إعادة تشغيل {fname}")
            try:
                await app.bot.send_message(
                    chat_id=int(uid),
                    text=f"🔧 تم اكتشاف مكتبة ناقصة `{bad_module}` وتثبيتها تلقائياً.\n"
                         f"🔄 جاري إعادة تشغيل `{fname}`...",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            return True
    except Exception as e:
        log.warning(f"auto-fix فشل: {e}")
    return False

# ─── قاعدة البيانات ───────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_FILE)
    c = con.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        uid TEXT PRIMARY KEY,
        plan TEXT DEFAULT 'free',
        joined TEXT,
        banned INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid TEXT,
        filename TEXT,
        size INTEGER,
        uploaded TEXT,
        runs INTEGER DEFAULT 0,
        crashes INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid TEXT,
        action TEXT,
        detail TEXT,
        ts TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    # إعدادات افتراضية
    defaults = [
        ("maintenance",   "0"),
        ("max_crashes",   "5"),
        ("rate_limit",    "8"),
        ("rate_window",   "30"),
        ("welcome_msg",   "ارفع، شغّل، وأدر بوتاتك بسهولة."),
        ("auto_restart",  "1"),
        ("notify_admin",  "1"),
    ]
    for k, v in defaults:
        c.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, v))
    try:
        c.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
    except Exception:
        pass
    con.commit()
    con.close()

def get_setting(key: str, default: str = "") -> str:
    con = db()
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    con.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    con = db()
    con.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
    con.commit()
    con.close()

def db():
    return sqlite3.connect(DB_FILE)

def get_user(uid: str):
    con = db()
    row = con.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
    con.close()
    return row

def ensure_user(uid: str):
    if not get_user(uid):
        con = db()
        con.execute("INSERT OR IGNORE INTO users VALUES (?,?,?,?)",
                    (uid, "free", datetime.now().isoformat(), 0))
        con.commit()
        con.close()

def set_plan(uid: str, plan: str):
    con = db()
    con.execute("UPDATE users SET plan=? WHERE uid=?", (plan, uid))
    con.commit()
    con.close()

def ban_user(uid: str):
    con = db()
    con.execute("UPDATE users SET banned=1 WHERE uid=?", (uid,))
    con.commit()
    con.close()

def unban_user(uid: str):
    con = db()
    con.execute("UPDATE users SET banned=0 WHERE uid=?", (uid,))
    con.commit()
    con.close()

def is_banned(uid: str) -> bool:
    con = db()
    row = con.execute("SELECT banned FROM users WHERE uid=?", (uid,)).fetchone()
    con.close()
    return bool(row and row[0])

def get_bots(uid: str):
    con = db()
    rows = con.execute("SELECT * FROM bots WHERE uid=?", (uid,)).fetchall()
    con.close()
    return rows

def add_bot_record(uid: str, filename: str, size: int):
    """أضف أو حدّث السجل — وأعد ضبط الأعطال لو نفس الاسم"""
    con = db()
    existing = con.execute(
        "SELECT id FROM bots WHERE uid=? AND filename=?", (uid, filename)
    ).fetchone()
    if existing:
        con.execute(
            "UPDATE bots SET size=?, uploaded=?, crashes=0 WHERE uid=? AND filename=?",
            (size, datetime.now().isoformat(), uid, filename)
        )
    else:
        con.execute(
            "INSERT INTO bots(uid,filename,size,uploaded) VALUES(?,?,?,?)",
            (uid, filename, size, datetime.now().isoformat())
        )
    con.commit()
    con.close()

def delete_bot_record(uid: str, filename: str):
    con = db()
    con.execute("DELETE FROM bots WHERE uid=? AND filename=?", (uid, filename))
    con.commit()
    con.close()

def inc_runs(uid: str, filename: str):
    con = db()
    con.execute("UPDATE bots SET runs=runs+1 WHERE uid=? AND filename=?", (uid, filename))
    con.commit()
    con.close()

def inc_crashes(uid: str, filename: str):
    con = db()
    con.execute("UPDATE bots SET crashes=crashes+1 WHERE uid=? AND filename=?", (uid, filename))
    con.commit()
    con.close()

def log_action(uid: str, action: str, detail: str = ""):
    con = db()
    con.execute("INSERT INTO logs(uid,action,detail,ts) VALUES(?,?,?,?)",
                (uid, action, detail, datetime.now().isoformat()))
    con.commit()
    con.close()
    log.info(f"[{uid}] {action} | {detail}")

# ─── الحالة في الذاكرة ───────────────────────────────────────────
running: dict[str, dict] = {}   # key = "uid:filename" → {process, uid, filename, started}
rate_limit: dict[str, list] = {}  # uid → [timestamps]  للحماية من الإرسال المتكرر

MAX_CRASHES   = 5    # أقصى عدد أعطال قبل إيقاف البوت نهائياً
RATE_LIMIT    = 8    # أقصى عدد رسائل في النافذة الزمنية
RATE_WINDOW   = 30   # النافذة الزمنية بالثواني
LOG_KEEP_DAYS = 30   # احتفظ بالسجلات لمدة 30 يوم فقط

# ─── فحص الكود ──────────────────────────────────────────────────
def is_code_safe(code: str) -> tuple[bool, str]:
    for pat in DANGER_PATTERNS:
        if re.search(pat, code, re.IGNORECASE):
            return False, f"نمط خطير: `{pat}`"
    return True, ""

def check_rate_limit(uid: str) -> bool:
    """يرجع True لو المستخدم تجاوز الحد — False لو مسموح"""
    now = time.time()
    times = rate_limit.get(uid, [])
    times = [t for t in times if now - t < RATE_WINDOW]
    rate_limit[uid] = times
    if len(times) >= RATE_LIMIT:
        return True
    rate_limit[uid].append(now)
    return False

def get_crash_count(uid: str, filename: str) -> int:
    con = db()
    row = con.execute("SELECT crashes FROM bots WHERE uid=? AND filename=?", (uid, filename)).fetchone()
    con.close()
    return row[0] if row else 0

def cleanup_old_logs():
    con = db()
    con.execute(
        "DELETE FROM logs WHERE ts < datetime('now', ?)",
        (f"-{LOG_KEEP_DAYS} days",)
    )
    con.commit()
    con.close()

# ─── لوحة التحكم ─────────────────────────────────────────────────
def main_panel(uid: str = ""):
    rows = [
        [InlineKeyboardButton("📂 ملفاتي",         callback_data="files"),
         InlineKeyboardButton("📤 رفع ملف",        callback_data="upload")],
        [InlineKeyboardButton("🔴 إيقاف ملف",      callback_data="stop"),
         InlineKeyboardButton("▶️ تشغيل ملف",      callback_data="run")],
        [InlineKeyboardButton("🔍 فحص ملف",        callback_data="inspect"),
         InlineKeyboardButton("🗑 حذف ملف",        callback_data="delete")],
        [InlineKeyboardButton("🔧 تثبيت مكتبة",    callback_data="user_install"),
         InlineKeyboardButton("⚡ سرعة البوت",     callback_data="stats")],
        [InlineKeyboardButton("ℹ️ شرح البوت",      callback_data="help_info")],
    ]
    # زر الأدمن يظهر فقط للأدمن
    if str(uid) == str(ADMIN):
        rows.append([InlineKeyboardButton("🛡 لوحة الأدمن",  callback_data="open_admin")])
    rows.append([
        InlineKeyboardButton("🔑 بوت ارقام وهميه", url=BOT_URL),
        InlineKeyboardButton("📢 قناة المطور",     url=CHANNEL_URL),
    ])
    return InlineKeyboardMarkup(rows)

def admin_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 المستخدمون",     callback_data="adm_users"),
         InlineKeyboardButton("📊 إحصائيات عامة",  callback_data="adm_stats")],
        [InlineKeyboardButton("🔧 تغيير باقة",      callback_data="adm_setplan"),
         InlineKeyboardButton("🚫 حظر مستخدم",      callback_data="adm_ban")],
        [InlineKeyboardButton("✅ رفع الحظر",        callback_data="adm_unban"),
         InlineKeyboardButton("📢 إرسال إشعار",      callback_data="adm_broadcast")],
        [InlineKeyboardButton("🤖 كل البوتات",       callback_data="adm_allbots"),
         InlineKeyboardButton("⛔ إيقاف بوت",        callback_data="adm_killbot")],
        [InlineKeyboardButton("⚙️ الإعدادات",        callback_data="adm_settings"),
         InlineKeyboardButton("📜 السجلات",          callback_data="adm_logs")],
        [InlineKeyboardButton("🔙 رجوع للمستخدم",    callback_data="back")],
    ])

def settings_panel():
    maintenance = "🟢 شغّال" if get_setting("maintenance") == "0" else "🔴 صيانة"
    auto_restart = "✅ مفعّل" if get_setting("auto_restart") == "1" else "❌ موقوف"
    notify      = "✅ مفعّل" if get_setting("notify_admin") == "1" else "❌ موقوف"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🏠 البوت: {maintenance}",      callback_data="cfg_toggle_maintenance")],
        [InlineKeyboardButton(f"🔄 إعادة تشغيل تلقائي: {auto_restart}", callback_data="cfg_toggle_restart")],
        [InlineKeyboardButton(f"🔔 إشعارات الرفع: {notify}",  callback_data="cfg_toggle_notify")],
        [InlineKeyboardButton("✏️ تعديل رسالة الترحيب",        callback_data="cfg_welcome")],
        [InlineKeyboardButton("🔢 حد الأعطال",                  callback_data="cfg_maxcrash"),
         InlineKeyboardButton("⏱ حد الرسائل",                   callback_data="cfg_ratelimit")],
        [InlineKeyboardButton("📦 تثبيت مكتبة يدوياً",          callback_data="cfg_install")],
        [InlineKeyboardButton("🔙 رجوع للأدمن",                  callback_data="adm_back")],
    ])

# ─── /start ──────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    ensure_user(uid)
    if is_banned(uid):
        await update.message.reply_text("🚫 تم حظرك من استخدام هذا البوت.")
        return
    if get_setting("maintenance") == "1" and update.effective_user.id != ADMIN:
        await update.message.reply_text("🔧 البوت في وضع الصيانة حالياً، عد لاحقاً.")
        return
    if check_rate_limit(uid):
        await update.message.reply_text("⏳ أرسلت كثيراً، انتظر قليلاً.")
        return
    log_action(uid, "start")
    name    = update.effective_user.first_name or "مستخدم"
    welcome = get_setting("welcome_msg", "ارفع، شغّل، وأدر بوتاتك بسهولة.")
    bots    = get_bots(uid)
    active  = sum(1 for b in bots if f"{uid}:{b[2]}" in running)
    plan_row = get_user(uid)
    plan_label = PLANS.get(plan_row[1], {}).get("label", "مجاني")
    await update.message.reply_text(
        f"🚀 *مرحباً بك {name}* في بوت استضافة وتشغيل ملفات Python 🐍\n\n"
        f"⚠️ *تنبيه هام:* يُمنع منعاً باتاً رفع أو تشغيل أي ملفات ضارة أو مشبوهة، "
        f"مخالفة سيتم التعامل معها بالحظر الفوري دون انذار.\n\n"
        f"📊 حسابك: *{plan_label}* | ملفات: `{len(bots)}` | نشط: `{active}`\n\n"
        f"⬇️ استخدم الازرار بالاسفل للتحكم في ملفاتك",
        parse_mode="Markdown",
        reply_markup=main_panel(uid)
    )

# ─── /help ───────────────────────────────────────────────────────
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if is_banned(uid):
        return
    text = (
        "📖 *الأوامر المتاحة:*\n\n"
        "/start — القائمة الرئيسية\n"
        "/status — حالة بوتاتك النشطة\n"
        "/mylogs — آخر 10 عمليات على حسابك\n"
        "/restart `<اسم_الملف>` — إعادة تشغيل بوت\n"
        "/help — هذه الرسالة\n"
    )
    if update.effective_user.id == ADMIN:
        text += "\n🛡 *أوامر الأدمن:*\n/admin — لوحة الأدمن\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# ─── /restart ────────────────────────────────────────────────────
async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if is_banned(uid):
        return
    args = context.args
    if not args:
        await update.message.reply_text("الاستخدام: /restart `<اسم_الملف>`", parse_mode="Markdown")
        return
    fname = args[0] if args[0].endswith(".py") else args[0] + ".py"
    key   = f"{uid}:{fname}"
    path  = f"{BOTS_DIR}/{uid}/{fname}"

    if not os.path.exists(path):
        await update.message.reply_text(f"❌ الملف `{fname}` غير موجود على الـ disk.", parse_mode="Markdown")
        return

    # أوقف القديم لو شغّال
    if key in running:
        running[key]["process"].terminate()
        del running[key]

    
        # منع تشغيل نفس البوت مرتين
        if key in running:
            try:
                if running[key]["process"].poll() is None:
                    running[key]["process"].terminate()
            except Exception:
                pass

        process = subprocess.Popen(
        ["python3", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    running[key] = {
        "process": process,
        "uid":      uid,
        "filename": fname,
        "started":  datetime.now().isoformat(),
    }
    inc_runs(uid, fname)
    log_action(uid, "restart_manual", fname)
    await update.message.reply_text(
        f"🔄 تم إعادة تشغيل `{fname}` (PID: {process.pid})",
        parse_mode="Markdown", reply_markup=main_panel(uid)
    )
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    ensure_user(uid)
    if is_banned(uid):
        await update.message.reply_text("🚫 تم حظرك.")
        return
    bots = get_bots(uid)
    if not bots:
        await update.message.reply_text("📭 لا يوجد بوتات مرفوعة.")
        return
    lines = ["🤖 *حالة بوتاتك:*\n"]
    for b in bots:
        key    = f"{uid}:{b[2]}"
        status = "🟢 يعمل" if key in running else "🔴 متوقف"
        uptime = ""
        if key in running:
            started = datetime.fromisoformat(running[key]["started"])
            diff    = datetime.now() - started
            h, rem  = divmod(int(diff.total_seconds()), 3600)
            m       = rem // 60
            uptime  = f" | ⏱ {h}س {m}د"
        lines.append(f"• `{b[2]}` {status}{uptime} | أعطال: {b[6]}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─── /mylogs ─────────────────────────────────────────────────────
async def mylogs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if is_banned(uid):
        return
    con  = db()
    rows = con.execute(
        "SELECT action, detail, ts FROM logs WHERE uid=? ORDER BY id DESC LIMIT 10",
        (uid,)
    ).fetchall()
    con.close()
    if not rows:
        await update.message.reply_text("📭 لا يوجد سجلات بعد.")
        return
    lines = ["📋 *آخر 10 عمليات لك:*\n"]
    for r in rows:
        lines.append(f"`{r[2][11:19]}` | {r[0]} | {r[1][:30]}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─── /admin ──────────────────────────────────────────────────────
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN:
        await update.message.reply_text("⛔ غير مصرح.")
        return
    await update.message.reply_text("🛡 لوحة الأدمن:", reply_markup=admin_panel())

# ─── الأزرار ─────────────────────────────────────────────────────
async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)
    ensure_user(uid)
    if is_banned(uid) and query.from_user.id != ADMIN:
        await query.message.reply_text("🚫 تم حظرك من استخدام هذا البوت.")
        return
    if check_rate_limit(uid) and query.from_user.id != ADMIN:
        await query.answer("⏳ أرسلت كثيراً، انتظر قليلاً.", show_alert=True)
        return
    data = query.data

    context.user_data["action"] = data

    # ── رجوع ──
    if data == "back":
        await query.message.reply_text("القائمة الرئيسية:", reply_markup=main_panel(uid))

    # ── فتح لوحة الأدمن من الرئيسية ──
    elif data == "open_admin":
        if query.from_user.id != ADMIN:
            await query.answer("⛔ غير مصرح.", show_alert=True)
            return
        await query.message.reply_text("🛡 لوحة الأدمن:", reply_markup=admin_panel())

    # ── رفع ──
    elif data == "upload":
        plan_row = get_user(uid)
        plan = plan_row[1]
        lim = PLANS[plan]
        await query.message.reply_text(
            f"📤 أرسل ملف `.py` الخاص ببوتك\n"
            f"باقتك: *{lim['label']}* | الحد: `{lim['size_mb']} MB` | عدد البوتات: `{lim['bots']}`",
            parse_mode="Markdown"
        )

    # ── ملفاتي ──
    elif data == "files":
        bots = get_bots(uid)
        if not bots:
            await query.message.reply_text("📭 لا يوجد ملفات مرفوعة بعد.")
            return
        lines = ["📂 *ملفاتك المرفوعة:*\n"]
        for b in bots:
            key = f"{uid}:{b[2]}"
            status = "🟢 يعمل" if key in running else "🔴 متوقف"
            size_kb = b[3] // 1024
            lines.append(f"• `{b[2]}` | {status} | {size_kb} KB | تشغيل: {b[5]}x | أعطال: {b[6]}x")
        await query.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # ── تشغيل ──
    elif data == "run":
        await query.message.reply_text("▶️ أرسل اسم الملف للتشغيل (مثال: `mybot.py`):", parse_mode="Markdown")

    # ── إيقاف ──
    elif data == "stop":
        active = [k.split(":")[1] for k in running if k.startswith(f"{uid}:")]
        if not active:
            await query.message.reply_text("⚠️ لا يوجد بوتات تعمل حالياً.")
            return
        btns = [[InlineKeyboardButton(f"⏹ {f}", callback_data=f"stop:{f}")] for f in active]
        btns.append([InlineKeyboardButton("🔙 رجوع", callback_data="back")])
        await query.message.reply_text("اختر البوت للإيقاف:", reply_markup=InlineKeyboardMarkup(btns))

    # ── إيقاف مباشر ──
    elif data.startswith("stop:"):
        fname = data[5:]
        key   = f"{uid}:{fname}"
        if key in running:
            running[key]["process"].terminate()
            del running[key]
            log_action(uid, "stop", fname)
            await query.message.reply_text(f"⏹ تم إيقاف `{fname}`", parse_mode="Markdown")
        else:
            await query.message.reply_text("⚠️ البوت غير موجود في القائمة النشطة.")

    # ── حذف ──
    elif data == "delete":
        bots = get_bots(uid)
        if not bots:
            await query.message.reply_text("📭 لا يوجد ملفات.")
            return
        btns = [[InlineKeyboardButton(f"🗑 {b[2]}", callback_data=f"del:{b[2]}")] for b in bots]
        btns.append([InlineKeyboardButton("🔙 رجوع", callback_data="back")])
        await query.message.reply_text("اختر البوت للحذف:", reply_markup=InlineKeyboardMarkup(btns))

    # ── حذف مباشر ──
    elif data.startswith("del:"):
        fname = data[4:]
        key   = f"{uid}:{fname}"
        path  = f"{BOTS_DIR}/{uid}/{fname}"
        if key in running:
            running[key]["process"].terminate()
            del running[key]
        if os.path.exists(path):
            os.remove(path)
        delete_bot_record(uid, fname)
        log_action(uid, "delete", fname)
        await query.message.reply_text(f"🗑 تم حذف `{fname}`", parse_mode="Markdown")

    # ── إحصائيات ──
    elif data == "stats":
        cpu  = psutil.cpu_percent(interval=0.5)
        ram  = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        active_count = sum(1 for k in running if k.startswith(f"{uid}:"))
        bots_count   = len(get_bots(uid))
        plan_row     = get_user(uid)
        plan         = plan_row[1]
        await query.message.reply_text(
            "📊 *إحصائيات السيرفر والحساب*\n\n"
            f"🖥 CPU: `{cpu}%`\n"
            f"💾 RAM: `{ram.percent}%` ({ram.used//1024//1024} / {ram.total//1024//1024} MB)\n"
            f"💿 Disk: `{disk.percent}%` ({disk.used//1024//1024//1024} / {disk.total//1024//1024//1024} GB)\n\n"
            f"🤖 بوتاتك: `{bots_count}` | نشط: `{active_count}`\n"
            f"💎 باقتك: *{PLANS[plan]['label']}*",
            parse_mode="Markdown"
        )

    # ── باقتي ──
    elif data == "myplan":
        plan_row = get_user(uid)
        plan     = plan_row[1]
        lines    = ["💎 *الباقات المتاحة:*\n"]
        for key, p in PLANS.items():
            mark = "✅" if key == plan else "  "
            lines.append(f"{mark} *{p['label']}* — {p['bots']} بوت | {p['size_mb']} MB | ${p['price']}/شهر")
        await query.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # ── فحص ملف ──
    elif data == "inspect":
        bots = get_bots(uid)
        if not bots:
            await query.message.reply_text("📭 لا يوجد ملفات.")
            return
        btns = [[InlineKeyboardButton(f"🔍 {b[2]}", callback_data=f"insp:{b[2]}")] for b in bots]
        btns.append([InlineKeyboardButton("🔙 رجوع", callback_data="back")])
        await query.message.reply_text("اختر الملف للفحص:", reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("insp:"):
        fname = data[5:]
        path  = f"{BOTS_DIR}/{uid}/{fname}"
        key   = f"{uid}:{fname}"
        if not os.path.exists(path):
            await query.message.reply_text(f"❌ الملف `{fname}` غير موجود على الـ disk.", parse_mode="Markdown")
            return
        size   = os.path.getsize(path)
        mtime  = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
        status = "🟢 يعمل" if key in running else "🔴 متوقف"
        uptime = ""
        if key in running:
            diff = datetime.now() - datetime.fromisoformat(running[key]["started"])
            h, r = divmod(int(diff.total_seconds()), 3600)
            uptime = f" | ⏱ {h}س {r//60}د"
        # اقرأ أول 5 سطور من الكود
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                preview = "\n".join(f.read().splitlines()[:5])
        except Exception:
            preview = "لا يمكن قراءة الكود"
        # استخرج المكتبات
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                imports = extract_imports(f.read())
            imports_text = ", ".join(imports[:10]) or "لا يوجد"
        except Exception:
            imports_text = "—"
        bdb = get_bots(uid)
        binfo = next((b for b in bdb if b[2] == fname), None)
        runs   = binfo[5] if binfo else 0
        crashes= binfo[6] if binfo else 0
        await query.message.reply_text(
            f"🔍 *فحص الملف: `{fname}`*\n\n"
            f"📌 الحالة: {status}{uptime}\n"
            f"💾 الحجم: `{size // 1024} KB`\n"
            f"🕒 آخر تعديل: `{mtime}`\n"
            f"▶️ تشغيل: `{runs}x` | 💥 أعطال: `{crashes}x`\n\n"
            f"📦 المكتبات: `{imports_text}`\n\n"
            f"📄 *أول 5 سطور:*\n```\n{preview}\n```",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ تشغيل", callback_data=f"run_direct:{fname}"),
                 InlineKeyboardButton("🗑 حذف",   callback_data=f"del:{fname}")],
                [InlineKeyboardButton("🔙 رجوع",  callback_data="back")],
            ])
        )

    elif data.startswith("run_direct:"):
        fname = data[11:]
        path  = f"{BOTS_DIR}/{uid}/{fname}"
        key   = f"{uid}:{fname}"
        if not os.path.exists(path):
            await query.message.reply_text(f"❌ الملف غير موجود.", parse_mode="Markdown")
            return
        if key in running:
            await query.message.reply_text("⚠️ البوت يعمل بالفعل.")
            return
        plan_row = get_user(uid)
        active = [k for k in running if k.startswith(f"{uid}:")]
        if len(active) >= PLANS[plan_row[1]]["bots"]:
            await query.message.reply_text("⛔ وصلت للحد الأقصى من البوتات النشطة.")
            return
        
        # منع تشغيل نفس البوت مرتين
        if key in running:
            try:
                if running[key]["process"].poll() is None:
                    running[key]["process"].terminate()
            except Exception:
                pass

        process = subprocess.Popen(["python3", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        running[key] = {"process": process, "uid": uid, "filename": fname, "started": datetime.now().isoformat()}
        inc_runs(uid, fname)
        log_action(uid, "run", fname)
        await query.message.reply_text(f"✅ تم تشغيل `{fname}` (PID: {process.pid})", parse_mode="Markdown", reply_markup=main_panel(uid))

    # ── تثبيت مكتبة (مستخدم) ──
    elif data == "user_install":
        bots = get_bots(uid)
        if not bots:
            await query.message.reply_text("📭 ارفع ملف أولاً حتى تثبّت مكتباته.")
            return
        btns = [[InlineKeyboardButton(f"📦 {b[2]}", callback_data=f"install_bot:{b[2]}")] for b in bots]
        btns.append([InlineKeyboardButton("✏️ تثبيت يدوي باسم", callback_data="install_manual")])
        btns.append([InlineKeyboardButton("🔙 رجوع", callback_data="back")])
        await query.message.reply_text("اختر الملف لتثبيت مكتباته تلقائياً:", reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("install_bot:"):
        fname = data[12:]
        path  = f"{BOTS_DIR}/{uid}/{fname}"
        if not os.path.exists(path):
            await query.message.reply_text("❌ الملف غير موجود.")
            return
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            packages = extract_imports(f.read())
        if not packages:
            await query.message.reply_text("✅ الملف لا يحتاج مكتبات خارجية.")
            return
        await query.message.reply_text(f"⏳ جاري تثبيت: `{'`, `'.join(packages)}`", parse_mode="Markdown")
        report = await install_requirements(packages, None)
        log_action(uid, "install", fname)
        await query.message.reply_text(f"📦 *نتيجة التثبيت:*\n{report}", parse_mode="Markdown", reply_markup=main_panel(uid))

    elif data == "install_manual":
        context.user_data["action"] = "user_install_manual"
        await query.message.reply_text("📦 أرسل اسم المكتبة أو أكثر مفصولة بمسافة:\nمثال: `requests aiohttp`", parse_mode="Markdown")

    # ── شرح البوت ──
    elif data == "help_info":
        welcome = get_setting("welcome_msg", "ارفع، شغّل، وأدر بوتاتك بسهولة.")
        await query.message.reply_text(
            "ℹ️ *شرح البوت*\n\n"
            "هذا البوت يتيح لك استضافة وتشغيل ملفات Python مباشرةً.\n\n"
            "📤 *رفع ملف* — ارفع ملف `.py` لتخزينه\n"
            "▶️ *تشغيل ملف* — شغّل أي ملف مرفوع\n"
            "🔴 *إيقاف ملف* — أوقف بوتاً نشطاً\n"
            "🔍 *فحص ملف* — تفاصيل الملف وأول 5 سطور\n"
            "🔧 *تثبيت مكتبة* — ثبّت مكتبات الملف تلقائياً\n"
            "⚡ *سرعة البوت* — إحصائيات السيرفر\n\n"
            f"💬 _{welcome}_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back")]])
        )
    elif data == "adm_users" and query.from_user.id == ADMIN:
        con  = db()
        rows = con.execute(
            "SELECT uid, plan, joined, banned FROM users ORDER BY joined DESC LIMIT 20"
        ).fetchall()
        total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        banned_count = con.execute("SELECT COUNT(*) FROM users WHERE banned=1").fetchone()[0]
        con.close()
        lines = [f"👥 *المستخدمون* | الكل: {total} | محظور: {banned_count}\n"]
        for r in rows:
            b = "🚫" if r[3] else "✅"
            lines.append(f"{b} `{r[0]}` | {r[1]} | {r[2][:10]}")
        await query.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=admin_panel())

    elif data == "adm_stats" and query.from_user.id == ADMIN:
        con = db()
        total_users  = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_bots   = con.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
        total_runs   = con.execute("SELECT SUM(runs) FROM bots").fetchone()[0] or 0
        total_crashes= con.execute("SELECT SUM(crashes) FROM bots").fetchone()[0] or 0
        plan_counts  = con.execute(
            "SELECT plan, COUNT(*) FROM users GROUP BY plan"
        ).fetchall()
        con.close()
        cpu  = psutil.cpu_percent(interval=0.5)
        ram  = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        active_total = len(running)
        plan_text = " | ".join([f"{p}: {c}" for p, c in plan_counts])
        await query.message.reply_text(
            "📊 *إحصائيات عامة للسيرفر*\n\n"
            f"👥 المستخدمون: `{total_users}`\n"
            f"🤖 البوتات: `{total_bots}` | نشط الآن: `{active_total}`\n"
            f"▶️ إجمالي التشغيل: `{total_runs}` | 💥 الأعطال: `{total_crashes}`\n"
            f"💎 الباقات: {plan_text}\n\n"
            f"🖥 CPU: `{cpu}%`\n"
            f"💾 RAM: `{ram.percent}%` ({ram.used//1024//1024}/{ram.total//1024//1024} MB)\n"
            f"💿 Disk: `{disk.percent}%` ({disk.used//1024//1024//1024}/{disk.total//1024//1024//1024} GB)",
            parse_mode="Markdown", reply_markup=admin_panel()
        )

    elif data == "adm_setplan" and query.from_user.id == ADMIN:
        context.user_data["action"] = "adm_setplan"
        await query.message.reply_text(
            "✏️ أرسل: `<uid> <plan>`\n"
            "الباقات: `free` | `basic` | `pro`\n"
            "مثال: `123456789 pro`",
            parse_mode="Markdown"
        )

    elif data == "adm_ban" and query.from_user.id == ADMIN:
        context.user_data["action"] = "adm_ban"
        await query.message.reply_text(
            "🚫 أرسل الـ UID للحظر:\nمثال: `123456789`",
            parse_mode="Markdown"
        )

    elif data == "adm_unban" and query.from_user.id == ADMIN:
        context.user_data["action"] = "adm_unban"
        await query.message.reply_text(
            "✅ أرسل الـ UID لرفع الحظر:\nمثال: `123456789`",
            parse_mode="Markdown"
        )

    elif data == "adm_broadcast" and query.from_user.id == ADMIN:
        context.user_data["action"] = "adm_broadcast"
        await query.message.reply_text(
            "📢 أرسل الرسالة للإرسال لكل المستخدمين:",
            parse_mode="Markdown"
        )

    elif data == "adm_allbots" and query.from_user.id == ADMIN:
        con  = db()
        rows = con.execute(
            "SELECT uid, filename, runs, crashes FROM bots ORDER BY runs DESC LIMIT 20"
        ).fetchall()
        con.close()
        lines = [f"🤖 *كل البوتات* (أكثر تشغيلاً):\n"]
        for r in rows:
            key    = f"{r[0]}:{r[1]}"
            status = "🟢" if key in running else "🔴"
            lines.append(f"{status} `{r[1]}` | uid:`{r[0][-6:]}` | ▶️{r[2]} 💥{r[3]}")
        await query.message.reply_text(
            "\n".join(lines) or "لا يوجد بوتات.",
            parse_mode="Markdown", reply_markup=admin_panel()
        )

    elif data == "adm_killbot" and query.from_user.id == ADMIN:
        if not running:
            await query.message.reply_text("⚠️ لا يوجد بوتات نشطة الآن.")
            return
        btns = [
            [InlineKeyboardButton(f"⛔ {k}", callback_data=f"adm_kill:{k}")]
            for k in list(running.keys())[:15]
        ]
        btns.append([InlineKeyboardButton("🔙 رجوع", callback_data="back")])
        await query.message.reply_text("اختر البوت للإيقاف القسري:", reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("adm_kill:") and query.from_user.id == ADMIN:
        key = data[9:]
        if key in running:
            running[key]["process"].terminate()
            uid_k = running[key]["uid"]
            fname_k = running[key]["filename"]
            del running[key]
            log_action(str(ADMIN), "adm_kill", key)
            try:
                await context.bot.send_message(
                    chat_id=int(uid_k),
                    text=f"⛔ تم إيقاف بوتك `{fname_k}` من قِبل الأدمن.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            await query.message.reply_text(f"⛔ تم إيقاف `{key}` بنجاح.", parse_mode="Markdown")
        else:
            await query.message.reply_text("⚠️ البوت لم يعد نشطاً.")

    elif data == "adm_logs" and query.from_user.id == ADMIN:
        con  = db()
        rows = con.execute(
            "SELECT uid, action, detail, ts FROM logs ORDER BY id DESC LIMIT 20"
        ).fetchall()
        con.close()
        lines = ["📜 *آخر 20 حدث:*\n"]
        for r in rows:
            lines.append(f"`{r[3][11:19]}` | `{r[0][-6:]}` | {r[1]} | {r[2][:30]}")
        await query.message.reply_text(
            "\n".join(lines) or "لا يوجد سجلات.",
            parse_mode="Markdown", reply_markup=admin_panel()
        )

    elif data == "adm_back" and query.from_user.id == ADMIN:
        await query.message.reply_text("🛡 لوحة الأدمن:", reply_markup=admin_panel())

    # ══ لوحة الإعدادات ══
    elif data == "adm_settings" and query.from_user.id == ADMIN:
        mc   = get_setting("max_crashes", "5")
        rl   = get_setting("rate_limit",  "8")
        rw   = get_setting("rate_window", "30")
        wm   = get_setting("welcome_msg", "")[:40]
        await query.message.reply_text(
            "⚙️ *إعدادات البانل*\n\n"
            f"🔢 حد الأعطال: `{mc}`\n"
            f"⏱ حد الرسائل: `{rl}` رسالة / `{rw}` ثانية\n"
            f"💬 رسالة الترحيب: _{wm}..._",
            parse_mode="Markdown",
            reply_markup=settings_panel()
        )

    elif data == "cfg_toggle_maintenance" and query.from_user.id == ADMIN:
        cur = get_setting("maintenance", "0")
        new = "0" if cur == "1" else "1"
        set_setting("maintenance", new)
        status = "🔴 وضع الصيانة مفعّل" if new == "1" else "🟢 البوت شغّال للجميع"
        log_action(str(ADMIN), "setting", f"maintenance → {new}")
        await query.message.reply_text(f"✅ {status}", reply_markup=settings_panel())

    elif data == "cfg_toggle_restart" and query.from_user.id == ADMIN:
        cur = get_setting("auto_restart", "1")
        new = "0" if cur == "1" else "1"
        set_setting("auto_restart", new)
        status = "✅ إعادة التشغيل التلقائي مفعّلة" if new == "1" else "❌ إعادة التشغيل التلقائي موقوفة"
        log_action(str(ADMIN), "setting", f"auto_restart → {new}")
        await query.message.reply_text(f"✅ {status}", reply_markup=settings_panel())

    elif data == "cfg_toggle_notify" and query.from_user.id == ADMIN:
        cur = get_setting("notify_admin", "1")
        new = "0" if cur == "1" else "1"
        set_setting("notify_admin", new)
        status = "✅ إشعارات الرفع مفعّلة" if new == "1" else "❌ إشعارات الرفع موقوفة"
        log_action(str(ADMIN), "setting", f"notify_admin → {new}")
        await query.message.reply_text(f"✅ {status}", reply_markup=settings_panel())

    elif data == "cfg_welcome" and query.from_user.id == ADMIN:
        context.user_data["action"] = "cfg_welcome"
        await query.message.reply_text(
            "✏️ أرسل رسالة الترحيب الجديدة:",
            parse_mode="Markdown"
        )

    elif data == "cfg_maxcrash" and query.from_user.id == ADMIN:
        context.user_data["action"] = "cfg_maxcrash"
        cur = get_setting("max_crashes", "5")
        await query.message.reply_text(
            f"🔢 حد الأعطال الحالي: `{cur}`\nأرسل الرقم الجديد (مثال: `3`):",
            parse_mode="Markdown"
        )

    elif data == "cfg_ratelimit" and query.from_user.id == ADMIN:
        context.user_data["action"] = "cfg_ratelimit"
        rl = get_setting("rate_limit",  "8")
        rw = get_setting("rate_window", "30")
        await query.message.reply_text(
            f"⏱ الإعداد الحالي: `{rl}` رسالة / `{rw}` ثانية\n"
            "أرسل بالصيغة: `<عدد_رسائل> <ثواني>`\nمثال: `10 60`",
            parse_mode="Markdown"
        )

    elif data == "cfg_install" and query.from_user.id == ADMIN:
        context.user_data["action"] = "cfg_install"
        await query.message.reply_text(
            "📦 أرسل اسم المكتبة أو أكثر (مفصولة بمسافة):\nمثال: `requests aiohttp pymongo`",
            parse_mode="Markdown"
        )

# ─── استقبال الملفات ──────────────────────────────────────────────
async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("action") != "upload":
        return

    uid  = str(update.effective_user.id)
    file = update.message.document

    if not file.file_name.endswith(".py"):
        await update.message.reply_text("⚠️ الملف يجب أن يكون `.py`")
        return

    plan_row = get_user(uid)
    plan     = plan_row[1]
    lim      = PLANS[plan]

    # فحص عدد البوتات (فقط لو الملف جديد — مش تحديث لملف موجود)
    current_bots = get_bots(uid)
    existing_names = [b[2] for b in current_bots]
    is_update = file.file_name in existing_names
    if not is_update and len(current_bots) >= lim["bots"]:
        await update.message.reply_text(
            f"⛔ وصلت للحد الأقصى ({lim['bots']} بوت) في باقتك.\n"
            "احذف بوتاً قديماً أو قم بترقية الباقة."
        )
        return

    # فحص الحجم
    size_mb = file.file_size / (1024 * 1024)
    if size_mb > lim["size_mb"]:
        await update.message.reply_text(
            f"⛔ حجم الملف كبير جداً ({size_mb:.1f} MB).\n"
            f"الحد المسموح: {lim['size_mb']} MB"
        )
        return

    # تحميل الملف
    path = f"{BOTS_DIR}/{uid}"
    os.makedirs(path, exist_ok=True)
    dest = f"{path}/{file.file_name}"
    tgfile = await file.get_file()
    await tgfile.download_to_drive(dest)

    # فحص الأمان
    try:
        with open(dest, "r", encoding="utf-8", errors="ignore") as f:
            code = f.read()
        safe, reason = is_code_safe(code)
        if not safe:
            os.remove(dest)
            log_action(uid, "rejected", f"{file.file_name} | {reason}")
            await update.message.reply_text(
                f"🚫 *تم رفض الملف* — يحتوي على كود غير مسموح:\n`{reason}`",
                parse_mode="Markdown"
            )
            return
    except Exception as e:
        log.warning(f"فشل فحص الكود: {e}")

    add_bot_record(uid, file.file_name, file.file_size)
    log_action(uid, "upload", file.file_name)

    action_label = "تحديث" if is_update else "رفع جديد"

    # تثبيت المكتبات تلقائياً
    try:
        with open(dest, "r", encoding="utf-8", errors="ignore") as f:
            code_text = f.read()
        packages = extract_imports(code_text)
        if packages:
            install_msg = await update.message.reply_text(
                f"⏳ جاري تثبيت المكتبات: `{'`, `'.join(packages)}`",
                parse_mode="Markdown"
            )
            report = await install_requirements(packages, install_msg)
            await update.message.reply_text(
                f"📦 *نتيجة تثبيت المكتبات:*\n{report}",
                parse_mode="Markdown"
            )
    except Exception as e:
        log.warning(f"فشل تثبيت المكتبات: {e}")

    await update.message.reply_text(
        f"✅ تم {action_label} `{file.file_name}` بنجاح!\n"
        "استخدم *▶️ تشغيل* لتشغيله.",
        parse_mode="Markdown",
        reply_markup=main_panel(uid)
    )

    # إشعار الأدمن بكل رفع جديد (مش تحديث)
    if not is_update and ADMIN and get_setting("notify_admin", "1") == "1":
        try:
            await context.bot.send_message(
                chat_id=ADMIN,
                text=f"📥 *رفع بوت جديد*\n"
                     f"المستخدم: `{uid}`\n"
                     f"الملف: `{file.file_name}`\n"
                     f"الحجم: {file.file_size // 1024} KB",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    context.user_data["action"] = None

# ─── الرسائل النصية ───────────────────────────────────────────────
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get("action")
    uid    = str(update.effective_user.id)
    msg    = update.message.text.strip()

    # ── تشغيل ──
    if action == "run":
        fname = msg if msg.endswith(".py") else msg + ".py"
        path  = f"{BOTS_DIR}/{uid}/{fname}"
        key   = f"{uid}:{fname}"

        # تحقق من وجود الملف فعلاً على الـ disk
        if not os.path.exists(path):
            await update.message.reply_text(
                f"❌ الملف `{fname}` غير موجود.\n"
                "ربما اتحذف — أعد رفعه من *📤 رفع بوت*",
                parse_mode="Markdown"
            )
            return

        if key in running:
            await update.message.reply_text("⚠️ البوت يعمل بالفعل.")
            return

        plan_row = get_user(uid)
        plan     = plan_row[1]
        active   = [k for k in running if k.startswith(f"{uid}:")]
        if len(active) >= PLANS[plan]["bots"]:
            await update.message.reply_text("⛔ وصلت للحد الأقصى من البوتات النشطة في باقتك.")
            return

        
        # منع تشغيل نفس البوت مرتين
        if key in running:
            try:
                if running[key]["process"].poll() is None:
                    running[key]["process"].terminate()
            except Exception:
                pass

        process = subprocess.Popen(
            ["python3", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        running[key] = {
            "process": process,
            "uid":      uid,
            "filename": fname,
            "started":  datetime.now().isoformat(),
        }
        inc_runs(uid, fname)
        log_action(uid, "run", fname)
        await update.message.reply_text(
            f"✅ تم تشغيل `{fname}` (PID: {process.pid})",
            parse_mode="Markdown",
            reply_markup=main_panel(uid)
        )
        context.user_data["action"] = None

    # ── إيقاف نصي ──
    elif action == "stop":
        fname = msg if msg.endswith(".py") else msg + ".py"
        key   = f"{uid}:{fname}"
        if key in running:
            running[key]["process"].terminate()
            del running[key]
            log_action(uid, "stop", fname)
            await update.message.reply_text(f"⏹ تم إيقاف `{fname}`", parse_mode="Markdown", reply_markup=main_panel(uid))
        else:
            await update.message.reply_text("⚠️ البوت غير مشغّل.")
        context.user_data["action"] = None

    # ── مستخدم: تثبيت مكتبة يدوي ──
    elif action == "user_install_manual":
        packages = msg.split()
        await update.message.reply_text(f"⏳ جاري تثبيت `{'`, `'.join(packages)}`...", parse_mode="Markdown")
        report = await install_requirements(packages, None)
        log_action(uid, "install_manual", msg)
        await update.message.reply_text(f"📦 *نتيجة التثبيت:*\n{report}", parse_mode="Markdown", reply_markup=main_panel(uid))
        context.user_data["action"] = None

    # ── أدمن: تغيير باقة ──
    elif action == "adm_setplan" and update.effective_user.id == ADMIN:
        parts = msg.split()
        if len(parts) != 2 or parts[1] not in PLANS:
            await update.message.reply_text("صيغة خاطئة. مثال: `123456789 pro`", parse_mode="Markdown")
            return
        target_uid, new_plan = parts
        ensure_user(target_uid)
        set_plan(target_uid, new_plan)
        log_action(str(ADMIN), "setplan", f"{target_uid} → {new_plan}")
        await update.message.reply_text(
            f"✅ تم تغيير باقة `{target_uid}` إلى *{PLANS[new_plan]['label']}*",
            parse_mode="Markdown", reply_markup=admin_panel()
        )
        context.user_data["action"] = None

    # ── أدمن: حظر ──
    elif action == "adm_ban" and update.effective_user.id == ADMIN:
        target = msg.strip()
        ensure_user(target)
        ban_user(target)
        # أوقف بوتاته
        stopped = 0
        for key in [k for k in running if k.startswith(f"{target}:")]:
            running[key]["process"].terminate()
            del running[key]
            stopped += 1
        log_action(str(ADMIN), "ban", target)
        try:
            await context.bot.send_message(
                chat_id=int(target),
                text="🚫 تم حظرك من استخدام البوت."
            )
        except Exception:
            pass
        await update.message.reply_text(
            f"🚫 تم حظر `{target}` وإيقاف {stopped} بوت.",
            parse_mode="Markdown", reply_markup=admin_panel()
        )
        context.user_data["action"] = None

    # ── أدمن: رفع الحظر ──
    elif action == "adm_unban" and update.effective_user.id == ADMIN:
        target = msg.strip()
        unban_user(target)
        log_action(str(ADMIN), "unban", target)
        try:
            await context.bot.send_message(
                chat_id=int(target),
                text="✅ تم رفع الحظر عنك. يمكنك استخدام البوت مجدداً."
            )
        except Exception:
            pass
        await update.message.reply_text(
            f"✅ تم رفع الحظر عن `{target}`",
            parse_mode="Markdown", reply_markup=admin_panel()
        )
        context.user_data["action"] = None

    # ── أدمن: تغيير رسالة الترحيب ──
    elif action == "cfg_welcome" and update.effective_user.id == ADMIN:
        set_setting("welcome_msg", msg)
        log_action(str(ADMIN), "setting", f"welcome_msg → {msg[:40]}")
        await update.message.reply_text("✅ تم تحديث رسالة الترحيب.", reply_markup=settings_panel())
        context.user_data["action"] = None

    # ── أدمن: تغيير حد الأعطال ──
    elif action == "cfg_maxcrash" and update.effective_user.id == ADMIN:
        if not msg.isdigit() or int(msg) < 1:
            await update.message.reply_text("⚠️ أرسل رقماً صحيحاً أكبر من 0.")
            return
        set_setting("max_crashes", msg)
        log_action(str(ADMIN), "setting", f"max_crashes → {msg}")
        await update.message.reply_text(f"✅ تم تعيين حد الأعطال إلى `{msg}`", parse_mode="Markdown", reply_markup=settings_panel())
        context.user_data["action"] = None

    # ── أدمن: تغيير حد الرسائل ──
    elif action == "cfg_ratelimit" and update.effective_user.id == ADMIN:
        parts = msg.split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            await update.message.reply_text("⚠️ صيغة خاطئة. مثال: `10 60`", parse_mode="Markdown")
            return
        set_setting("rate_limit",  parts[0])
        set_setting("rate_window", parts[1])
        log_action(str(ADMIN), "setting", f"rate → {parts[0]}/{parts[1]}s")
        await update.message.reply_text(f"✅ حد الرسائل: `{parts[0]}` كل `{parts[1]}` ثانية", parse_mode="Markdown", reply_markup=settings_panel())
        context.user_data["action"] = None

    # ── أدمن: تثبيت مكتبة يدوياً ──
    elif action == "cfg_install" and update.effective_user.id == ADMIN:
        packages = msg.split()
        status_msg = await update.message.reply_text(f"⏳ جاري تثبيت {len(packages)} مكتبة...")
        report = await install_requirements(packages, status_msg)
        log_action(str(ADMIN), "manual_install", msg)
        await update.message.reply_text(f"📦 *نتيجة التثبيت:*\n{report}", parse_mode="Markdown", reply_markup=settings_panel())
        context.user_data["action"] = None
    elif action == "adm_broadcast" and update.effective_user.id == ADMIN:
        con  = db()
        uids = [r[0] for r in con.execute("SELECT uid FROM users WHERE banned=0").fetchall()]
        con.close()
        success, fail = 0, 0
        for u in uids:
            try:
                await context.bot.send_message(
                    chat_id=int(u),
                    text=f"📢 *رسالة من الإدارة:*\n\n{msg}",
                    parse_mode="Markdown"
                )
                success += 1
            except Exception:
                fail += 1
        log_action(str(ADMIN), "broadcast", f"✅{success} ❌{fail}")
        await update.message.reply_text(
            f"📢 تم الإرسال!\n✅ نجح: `{success}` | ❌ فشل: `{fail}`",
            parse_mode="Markdown", reply_markup=admin_panel()
        )
        context.user_data["action"] = None

# ─── المراقب التلقائي ──────────────────────────────────────────────
async def monitor(app: Application):
    cycle = 0
    while True:
        for key, info in list(running.items()):
            proc = info["process"]
            if proc.poll() is not None:
                uid        = info["uid"]
                fname      = info["filename"]
                path       = f"{BOTS_DIR}/{uid}/{fname}"
                returncode = proc.returncode

                # اقرأ آخر سطر من stderr للتشخيص
                last_error = ""
                try:
                    raw = proc.stderr.read(2048)
                    if raw:
                        lines = raw.decode("utf-8", errors="ignore").strip().splitlines()
                        last_error = lines[-1][:200] if lines else ""
                except Exception:
                    pass

                # ── إيقاف طبيعي (exit 0 أو SIGTERM = -15) ──
                if returncode in (0, -15):
                    log.info(f"البوت {key} توقف طبيعياً (code {returncode})")
                    del running[key]
                    continue

                # ── تحقق هل الخطأ ImportError → حاول تثبيت تلقائي ──
                is_import_error = "ImportError" in last_error or "No module named" in last_error
                if is_import_error:
                    fixed = await auto_fix_imports(uid, fname, last_error, app)
                    if fixed and os.path.exists(path):
                        # أعد التشغيل مباشرة بدون عدّ عطل
                        new_proc = subprocess.Popen(
                            ["python3", path],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE
                        )
                        running[key]["process"] = new_proc
                        log_action(uid, "autofix_restart", f"{fname} | {last_error[:60]}")
                        continue   # لا تعدّ كعطل

                # ── كراش حقيقي ──
                inc_crashes(uid, fname)
                crash_count = get_crash_count(uid, fname)
                max_c = int(get_setting("max_crashes", str(MAX_CRASHES)))
                log.warning(f"كراش {key} | code={returncode} | أعطال={crash_count}/{max_c} | {last_error}")

                if crash_count >= max_c:
                    del running[key]
                    log_action(uid, "disabled", f"{fname} | {crash_count} أعطال | {last_error[:80]}")
                    try:
                        await app.bot.send_message(
                            chat_id=int(uid),
                            text=f"⛔ تم إيقاف `{fname}` نهائياً بسبب تجاوز حد الأعطال ({max_c} مرة).\n"
                                 f"آخر خطأ:\n`{last_error}`\n\nراجع الكود وأعد رفعه.",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                elif os.path.exists(path) and get_setting("auto_restart", "1") == "1":
                    new_proc = subprocess.Popen(
                        ["python3", path],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    running[key]["process"] = new_proc
                    log_action(uid, "restart", f"{fname} | عطل {crash_count}/{max_c}")
                    try:
                        await app.bot.send_message(
                            chat_id=int(uid),
                            text=f"🔄 تم إعادة تشغيل `{fname}` تلقائياً.\n"
                                 f"⚠️ عدد الأعطال: {crash_count}/{max_c}\n"
                                 f"آخر خطأ: `{last_error}`",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                else:
                    del running[key]

        # تنظيف الـ logs القديمة كل 1000 دورة (~2.7 ساعة)
        cycle += 1
        if cycle % 1000 == 0:
            cleanup_old_logs()
            log.info("🧹 تم تنظيف السجلات القديمة")

        await asyncio.sleep(15)

# ─── main ────────────────────────────────────────────────────────
async def post_init(app: Application):
    """يتشغل بعد ما run_polling يجهّز الـ event loop تماماً"""
    asyncio.create_task(monitor(app))
    log.info("✅ المراقب التلقائي شتغل")

def main():
    init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)   # ← هنا بدل create_task في main()
        .build()
    )

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("admin",   admin_cmd))
    app.add_handler(CommandHandler("status",  status_cmd))
    app.add_handler(CommandHandler("mylogs",  mylogs_cmd))
    app.add_handler(CommandHandler("help",    help_cmd))
    app.add_handler(CommandHandler("restart", restart_cmd))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.Document.ALL, upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    log.info("✅ البوت شتغل بنجاح")
    app.run_polling()   # ← مش await، هي بتدير الـ loop بنفسها

if __name__ == "__main__":
    main()   # ← مش asyncio.run()
