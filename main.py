
import os
import asyncio
import signal
import subprocess
import psutil
import sqlite3
from datetime import datetime
from collections import deque

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN = int(os.getenv("ADMIN_ID", "0"))

BOTS_DIR = "bots"
DB_FILE = "panel.db"

# limits
RAM_LIMIT_MB = 300   # max RAM per bot
CHECK_INTERVAL = 10

running = {}
queue = deque()

def db():
    return sqlite3.connect(DB_FILE)

def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS bots(
        uid TEXT,
        filename TEXT,
        uploaded TEXT
    )""")
    con.commit()
    con.close()

def add_bot(uid, fname):
    con = db()
    con.execute("INSERT INTO bots VALUES (?,?,?)",
                (uid, fname, datetime.now().isoformat()))
    con.commit()
    con.close()

def get_bots(uid):
    con = db()
    rows = con.execute("SELECT filename FROM bots WHERE uid=?", (uid,)).fetchall()
    con.close()
    return [r[0] for r in rows]

def log_path(uid, fname):
    os.makedirs("logs", exist_ok=True)
    return f"logs/{uid}_{fname}.log"

def kill_process(key):
    if key in running:
        try:
            os.killpg(os.getpgid(running[key]["process"].pid), signal.SIGTERM)
        except:
            pass
        del running[key]

def enqueue_start(uid, fname):
    queue.append((uid, fname))

def start_bot(uid, fname):
    path = f"{BOTS_DIR}/{uid}/{fname}"
    key = f"{uid}:{fname}"

    kill_process(key)

    logf = open(log_path(uid, fname), "ab")

    proc = subprocess.Popen(
        ["python3", path],
        stdout=logf,
        stderr=logf,
        preexec_fn=os.setsid
    )

    running[key] = {
        "process": proc,
        "uid": uid,
        "fname": fname,
        "start": datetime.now()
    }

def stop_bot(uid, fname):
    key = f"{uid}:{fname}"
    kill_process(key)

def panel(uid):
    rows = [
        [InlineKeyboardButton("📂 ملفاتي", callback_data="files"),
         InlineKeyboardButton("📤 رفع", callback_data="upload")],

        [InlineKeyboardButton("▶️ تشغيل", callback_data="run"),
         InlineKeyboardButton("⏹ إيقاف", callback_data="stop")],

        [InlineKeyboardButton("📊 السيرفر", callback_data="stats")]
    ]

    if int(uid) == ADMIN:
        rows.append([InlineKeyboardButton("🛡 Admin", callback_data="admin")])

    return InlineKeyboardMarkup(rows)

def admin_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 كل البوتات", callback_data="allbots")],
        [InlineKeyboardButton("📊 السيرفر", callback_data="stats")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="back")]
    ])

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=str(update.effective_user.id)
    os.makedirs(f"{BOTS_DIR}/{uid}",exist_ok=True)
    await update.message.reply_text(
        "🚀 لوحة استضافة البوتات",
        reply_markup=panel(uid)
    )

async def buttons(update:Update,context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()

    uid=str(q.from_user.id)
    data=q.data

    if data=="files":
        bots=get_bots(uid)
        if not bots:
            await q.message.reply_text("لا يوجد بوتات")
            return

        txt="بوتاتك:\n"
        for b in bots:
            key=f"{uid}:{b}"
            st="🟢 يعمل" if key in running else "🔴 متوقف"
            txt+=f"{b} {st}\n"

        await q.message.reply_text(txt)

    elif data=="run":
        context.user_data["mode"]="run"
        await q.message.reply_text("أرسل اسم الملف")

    elif data=="stop":
        context.user_data["mode"]="stop"
        await q.message.reply_text("أرسل اسم الملف")

    elif data=="upload":
        context.user_data["mode"]="upload"
        await q.message.reply_text("أرسل ملف .py")

    elif data=="stats":
        cpu=psutil.cpu_percent()
        ram=psutil.virtual_memory()
        await q.message.reply_text(
            f"CPU {cpu}%\nRAM {ram.percent}%\nRunning bots {len(running)}"
        )

    elif data=="admin" and int(uid)==ADMIN:
        await q.message.reply_text("Admin panel",reply_markup=admin_panel())

    elif data=="allbots" and int(uid)==ADMIN:
        txt="Running:\n"
        for k in running:
            txt+=k+"\n"
        await q.message.reply_text(txt or "None")

async def text(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=str(update.effective_user.id)
    mode=context.user_data.get("mode")
    msg=update.message.text

    if mode=="run":
        enqueue_start(uid,msg)
        await update.message.reply_text("⏳ أضيف إلى قائمة التشغيل")

    elif mode=="stop":
        stop_bot(uid,msg)
        await update.message.reply_text("تم الإيقاف")

async def upload(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("mode")!="upload":
        return

    uid=str(update.effective_user.id)
    doc=update.message.document

    if not doc.file_name.endswith(".py"):
        await update.message.reply_text("الملف يجب أن يكون .py")
        return

    os.makedirs(f"{BOTS_DIR}/{uid}",exist_ok=True)

    f=await doc.get_file()
    dest=f"{BOTS_DIR}/{uid}/{doc.file_name}"
    await f.download_to_drive(dest)

    add_bot(uid,doc.file_name)

    await update.message.reply_text("تم رفع الملف")

async def monitor():
    while True:

        # start queued bots
        if queue:
            uid,fname=queue.popleft()
            start_bot(uid,fname)

        for key,data in list(running.items()):
            proc=data["process"]

            if proc.poll() is not None:
                del running[key]
                continue

            try:
                p=psutil.Process(proc.pid)
                mem=p.memory_info().rss/1024/1024

                if mem>RAM_LIMIT_MB:
                    kill_process(key)

            except:
                pass

        await asyncio.sleep(CHECK_INTERVAL)

async def post_init(app):
    asyncio.create_task(monitor())

def main():
    init_db()

    app=(
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.Document.ALL,upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text))

    app.run_polling()

if __name__=="__main__":
    main()
