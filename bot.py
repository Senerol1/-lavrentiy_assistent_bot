import logging
import sqlite3
import re
import os
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# ── Состояния бота ──
STATE_IDLE             = None
STATE_WAITING_TASKS    = "waiting_tasks"
STATE_WAITING_CARRYOVER = "waiting_carryover"
STATE_WAITING_REM_TEXT = "waiting_rem_text"
STATE_WAITING_REM_DATE = "waiting_rem_date"

bot_state = {"mode": STATE_IDLE, "temp": {}}


# ════════════════════════════════════
#  БАЗА ДАННЫХ
# ════════════════════════════════════

def init_db():
    with sqlite3.connect("bot.db") as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                text         TEXT    NOT NULL,
                done         INTEGER DEFAULT 0,
                date         TEXT    NOT NULL,
                original_date TEXT
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                text      TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                sent      INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)

def get_setting(key):
    with sqlite3.connect("bot.db") as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None

def set_setting(key, value):
    with sqlite3.connect("bot.db") as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))

def today_str():
    return datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")

def tomorrow_str():
    return (datetime.now(MOSCOW_TZ) + timedelta(days=1)).strftime("%d.%m.%Y")

def get_tasks(date):
    with sqlite3.connect("bot.db") as c:
        return c.execute(
            "SELECT id, text, done, original_date FROM tasks WHERE date=? ORDER BY id",
            (date,)
        ).fetchall()


# ════════════════════════════════════
#  КЛАВИАТУРА-ЧЕКЛИСТ
# ════════════════════════════════════

def build_keyboard(date):
    tasks = get_tasks(date)
    if not tasks:
        return None
    rows = []
    for tid, text, done, orig in tasks:
        icon  = "✅" if done else "⬜"
        label = f"{icon} {text}"
        if orig and orig != date:
            label += f"  (перенос с {orig})"
        rows.append([InlineKeyboardButton(label, callback_data=f"t_{tid}")])
    return InlineKeyboardMarkup(rows)


# ════════════════════════════════════
#  КОМАНДЫ
# ════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_setting("chat_id", str(update.effective_chat.id))
    await update.message.reply_text(
        "👋 *Привет! Я твой личный планировщик.*\n\n"
        "📅 *Расписание:*\n"
        "• 10:00 — запрошу список задач на день\n"
        "• 12:00 — напомню выпить таблетки 💊\n"
        "• 18:00 — спрошу что перенести на завтра\n\n"
        "📋 /newtasks — добавить задачи прямо сейчас\n"
        "📋 /tasks — чеклист на сегодня\n"
        "🔔 /reminder — добавить напоминание\n"
        "📆 /reminders — все напоминания",
        parse_mode="Markdown"
    )

async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = today_str()
    kb = build_keyboard(today)
    if not kb:
        await update.message.reply_text("На сегодня задач пока нет.")
        return
    await update.message.reply_text(
        f"📋 *Задачи на {today}:*\n_(нажми чтобы отметить)_",
        reply_markup=kb, parse_mode="Markdown"
    )

async def cmd_newtasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_state["mode"] = STATE_WAITING_TASKS
    await update.message.reply_text("📝 Пришли список задач на сегодня — каждая с новой строки:")

async def cmd_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_state["mode"] = STATE_WAITING_REM_TEXT
    await update.message.reply_text("📝 Напиши текст напоминания:")

async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect("bot.db") as c:
        rows = c.execute(
            "SELECT text, remind_at FROM reminders WHERE sent=0 ORDER BY remind_at"
        ).fetchall()
    if not rows:
        await update.message.reply_text("Нет предстоящих напоминаний.")
        return
    lines = [f"🔔 *{r[0]}*\n   🕐 {r[1][:16].replace('T',' ')} (МСК)" for r in rows]
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


# ════════════════════════════════════
#  НАЖАТИЕ НА ЗАДАЧУ (toggle)
# ════════════════════════════════════

async def handle_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tid = int(query.data.split("_")[1])

    with sqlite3.connect("bot.db") as c:
        row = c.execute("SELECT done, date FROM tasks WHERE id=?", (tid,)).fetchone()
        if row:
            c.execute("UPDATE tasks SET done=? WHERE id=?", (1 - row[0], tid))
            date = row[1]

    kb = build_keyboard(date)
    if kb:
        await query.edit_message_reply_markup(reply_markup=kb)


# ════════════════════════════════════
#  ОБРАБОТЧИК ТЕКСТА
# ════════════════════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    mode = bot_state["mode"]
    set_setting("chat_id", str(update.effective_chat.id))

    # ── Получение задач утром ──
    if mode == STATE_WAITING_TASKS:
        today = today_str()
        lines = [
            re.sub(r'^[\d]+[.)]\s*|^[-–•*]\s*', '', l.strip())
            for l in text.split("\n")
        ]
        tasks = [l for l in lines if l]

        if not tasks:
            await update.message.reply_text("Не смог разобрать список. Каждая задача — с новой строки.")
            return

        with sqlite3.connect("bot.db") as c:
            c.execute(
                "DELETE FROM tasks WHERE date=? AND (original_date IS NULL OR original_date=?)",
                (today, today)
            )
            for t in tasks:
                c.execute(
                    "INSERT INTO tasks (text, date, original_date) VALUES (?,?,?)",
                    (t, today, today)
                )

        bot_state["mode"] = STATE_IDLE
        kb = build_keyboard(today)
        await update.message.reply_text(
            f"✅ Добавил {len(tasks)} задач на сегодня.\n_(нажми чтобы отметить выполненные)_",
            reply_markup=kb, parse_mode="Markdown"
        )

    # ── Перенос задач вечером ──
    elif mode == STATE_WAITING_CARRYOVER:
        today    = today_str()
        tomorrow = tomorrow_str()
        low = text.lower()

        with sqlite3.connect("bot.db") as c:
            undone = c.execute(
                "SELECT id, text, original_date FROM tasks WHERE date=? AND done=0",
                (today,)
            ).fetchall()

        if not undone or low in ["нет", "ничего", "-", "no", "0"]:
            bot_state["mode"] = STATE_IDLE
            await update.message.reply_text("Хорошо, ничего не переносим. Хорошего вечера! 🌙")
            return

        if low in ["все", "всё", "да", "all", "yes"]:
            to_carry = undone
        else:
            nums = [int(n) - 1 for n in re.findall(r'\d+', text)]
            to_carry = [undone[i] for i in nums if 0 <= i < len(undone)]

        if to_carry:
            with sqlite3.connect("bot.db") as c:
                for _, t, orig in to_carry:
                    c.execute(
                        "INSERT INTO tasks (text, date, original_date) VALUES (?,?,?)",
                        (t, tomorrow, orig or today)
                    )
            names = "\n".join(f"• {t[1]}" for t in to_carry)
            await update.message.reply_text(
                f"📅 Перенёс на завтра ({tomorrow}):\n{names}\n\nХорошего вечера! 🌙"
            )
        else:
            await update.message.reply_text("Не понял номера, ничего не перенёс. Хорошего вечера! 🌙")

        bot_state["mode"] = STATE_IDLE

    # ── Текст напоминания ──
    elif mode == STATE_WAITING_REM_TEXT:
        bot_state["temp"]["rem_text"] = text
        bot_state["mode"] = STATE_WAITING_REM_DATE
        await update.message.reply_text(
            "📅 Когда напомнить? Формат:\n`ДД.ММ.ГГГГ ЧЧ:ММ`\nПример: `25.04.2025 14:30`",
            parse_mode="Markdown"
        )

    # ── Дата/время напоминания ──
    elif mode == STATE_WAITING_REM_DATE:
        try:
            dt = datetime.strptime(text, "%d.%m.%Y %H:%M")
            dt = MOSCOW_TZ.localize(dt)
            rem_text = bot_state["temp"].get("rem_text", "")

            with sqlite3.connect("bot.db") as c:
                c.execute(
                    "INSERT INTO reminders (text, remind_at) VALUES (?,?)",
                    (rem_text, dt.isoformat())
                )

            bot_state.update({"mode": STATE_IDLE, "temp": {}})
            await update.message.reply_text(
                f"✅ Напоминание сохранено!\n📝 {rem_text}\n🕐 {text} (МСК)"
            )
        except ValueError:
            await update.message.reply_text(
                "❌ Неверный формат. Попробуй: `ДД.ММ.ГГГГ ЧЧ:ММ`",
                parse_mode="Markdown"
            )


# ════════════════════════════════════
#  ПЛАНИРОВЩИК
# ════════════════════════════════════

async def job_morning(app):
    chat_id = get_setting("chat_id")
    if not chat_id:
        return
    bot_state["mode"] = STATE_WAITING_TASKS

    today   = today_str()
    carried = [t for t in get_tasks(today) if t[3] and t[3] != today]

    msg = "🌅 *Доброе утро!* Пришли список задач на сегодня — я превращу их в чеклист."
    if carried:
        lines = "\n".join(f"• {t[1]}  _(с {t[3]})_" for t in carried)
        msg += f"\n\nУже перенесено со вчера:\n{lines}"

    await app.bot.send_message(chat_id=int(chat_id), text=msg, parse_mode="Markdown")

async def job_pills(app):
    chat_id = get_setting("chat_id")
    if not chat_id:
        return
    await app.bot.send_message(chat_id=int(chat_id), text="💊 Время выпить таблетки!")

async def job_evening(app):
    chat_id = get_setting("chat_id")
    if not chat_id:
        return

    today  = today_str()
    undone = [t for t in get_tasks(today) if t[2] == 0]

    if not undone:
        await app.bot.send_message(
            chat_id=int(chat_id),
            text="🌆 Отличный день — все задачи выполнены! 🎉"
        )
        return

    bot_state["mode"] = STATE_WAITING_CARRYOVER
    lines = "\n".join(f"{i+1}. {t[1]}" for i, t in enumerate(undone))
    await app.bot.send_message(
        chat_id=int(chat_id),
        text=(
            f"🌆 *Добрый вечер!* Невыполненные задачи:\n\n{lines}\n\n"
            "Что перенести на завтра?\n"
            "• Напиши *все* — перенести всё\n"
            "• Номера через запятую: *1, 3*\n"
            "• *нет* — ничего не переносить"
        ),
        parse_mode="Markdown"
    )

async def job_reminders(app):
    chat_id = get_setting("chat_id")
    if not chat_id:
        return

    now = datetime.now(MOSCOW_TZ)
    with sqlite3.connect("bot.db") as c:
        rows = c.execute(
            "SELECT id, text, remind_at FROM reminders WHERE sent=0"
        ).fetchall()
        for rid, text, rat in rows:
            try:
                dt = datetime.fromisoformat(rat)
                if dt.tzinfo is None:
                    dt = MOSCOW_TZ.localize(dt)
                if now >= dt:
                    await app.bot.send_message(
                        chat_id=int(chat_id),
                        text=f"🔔 *Напоминание:* {text}",
                        parse_mode="Markdown"
                    )
                    c.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
            except Exception as e:
                logging.error(f"Reminder error: {e}")


# ════════════════════════════════════
#  ЗАПУСК
# ════════════════════════════════════

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("tasks",     cmd_tasks))
    app.add_handler(CommandHandler("newtasks",  cmd_newtasks))
    app.add_handler(CommandHandler("reminder",  cmd_reminder))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CallbackQueryHandler(handle_toggle, pattern=r"^t_\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    scheduler.add_job(job_morning,   "cron", hour=10, minute=0,  args=[app])
    scheduler.add_job(job_pills,     "cron", hour=12, minute=0,  args=[app])
    scheduler.add_job(job_evening,   "cron", hour=18, minute=0,  args=[app])
    scheduler.add_job(job_reminders, "interval", minutes=1,      args=[app])
    scheduler.start()

    logging.info("✅ Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
