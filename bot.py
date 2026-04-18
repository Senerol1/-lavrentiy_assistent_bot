import logging
import re
import os
from datetime import datetime, timedelta
from contextlib import contextmanager
import pytz
import psycopg2
import psycopg2.extras
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

BOT_TOKEN    = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
MOSCOW_TZ    = pytz.timezone('Europe/Moscow')

# ── Состояния ──
STATE_IDLE             = None
STATE_WAITING_TASKS    = "waiting_tasks"
STATE_WAITING_CARRYOVER = "waiting_carryover"
STATE_EDITING_TASK     = "editing_task"
STATE_ADDING_NOTE      = "adding_note"
STATE_ADDING_HABIT     = "adding_habit"
STATE_WAITING_REM_TEXT = "waiting_rem_text"
STATE_WAITING_REM_DATE = "waiting_rem_date"

bot_state = {"mode": STATE_IDLE, "temp": {}}


# ════════════════════════════════════
#  БАЗА ДАННЫХ (PostgreSQL)
# ════════════════════════════════════

@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def db_execute(query, params=()):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)

def db_fetchone(query, params=()):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()

def db_fetchall(query, params=()):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id            SERIAL PRIMARY KEY,
                    text          TEXT    NOT NULL,
                    done          INTEGER DEFAULT 0,
                    priority      INTEGER DEFAULT 0,
                    date          TEXT    NOT NULL,
                    original_date TEXT,
                    completed_at  TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id        SERIAL PRIMARY KEY,
                    text      TEXT NOT NULL,
                    remind_at TEXT NOT NULL,
                    sent      INTEGER DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notes (
                    id         SERIAL PRIMARY KEY,
                    text       TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS habits (
                    id     SERIAL PRIMARY KEY,
                    name   TEXT NOT NULL,
                    active INTEGER DEFAULT 1
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS habit_log (
                    id       SERIAL PRIMARY KEY,
                    habit_id INTEGER NOT NULL,
                    date     TEXT    NOT NULL,
                    UNIQUE(habit_id, date)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            # Миграции
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS completed_at TEXT")

def get_setting(key):
    row = db_fetchone("SELECT value FROM settings WHERE key=%s", (key,))
    return row[0] if row else None

def set_setting(key, value):
    db_execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        (key, str(value))
    )

def today_str():
    return datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")

def tomorrow_str():
    return (datetime.now(MOSCOW_TZ) + timedelta(days=1)).strftime("%d.%m.%Y")

def get_tasks(date, done=None):
    q = "SELECT id, text, done, priority, original_date FROM tasks WHERE date=%s"
    p = [date]
    if done is not None:
        q += " AND done=%s"
        p.append(done)
    q += " ORDER BY priority DESC, id"
    return db_fetchall(q, p)

def get_habits():
    return db_fetchall("SELECT id, name FROM habits WHERE active=1 ORDER BY id")

def is_habit_done(habit_id, date):
    return bool(db_fetchone(
        "SELECT 1 FROM habit_log WHERE habit_id=%s AND date=%s", (habit_id, date)
    ))

def get_streak(habit_id):
    rows = db_fetchall(
        "SELECT date FROM habit_log WHERE habit_id=%s ORDER BY date DESC", (habit_id,)
    )
    if not rows:
        return 0
    streak   = 0
    expected = datetime.now(MOSCOW_TZ).date()
    for (d_str,) in rows:
        d = datetime.strptime(d_str, "%d.%m.%Y").date()
        if d == expected:
            streak  += 1
            expected -= timedelta(days=1)
        else:
            break
    return streak


# ════════════════════════════════════
#  КЛАВИАТУРЫ
# ════════════════════════════════════

def main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📋 Задачи"),      KeyboardButton("➕ Новые задачи")],
            [KeyboardButton("✅ Выполненные"),  KeyboardButton("💪 Привычки")],
            [KeyboardButton("📓 Заметки"),      KeyboardButton("🔔 Напоминание")],
            [KeyboardButton("📆 Напоминания"),  KeyboardButton("📊 Обзор недели")],
        ],
        resize_keyboard=True
    )

def build_task_kb(date):
    tasks = get_tasks(date, done=0)
    if not tasks:
        return None
    rows = []
    for tid, text, _, priority, orig in tasks:
        label = ("🔴 " if priority else "⬜ ") + text
        if orig and orig != date:
            label += f"  (с {orig})"
        rows.append([InlineKeyboardButton(label, callback_data=f"done_{tid}")])
    rows.append([
        InlineKeyboardButton("✏️ Изменить",  callback_data="act_edit"),
        InlineKeyboardButton("🗑 Удалить",   callback_data="act_delete"),
        InlineKeyboardButton("➡️ На завтра", callback_data="act_postpone"),
    ])
    return InlineKeyboardMarkup(rows)

def build_select_kb(date, action):
    tasks = get_tasks(date, done=0)
    rows  = []
    for i, (tid, text, _, priority, _) in enumerate(tasks):
        rows.append([InlineKeyboardButton(f"{i+1}. {text}", callback_data=f"{action}_{tid}")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="act_cancel")])
    return InlineKeyboardMarkup(rows)

def build_habit_kb(date):
    habits = get_habits()
    rows   = []
    if habits:
        for hid, name in habits:
            done   = is_habit_done(hid, date)
            streak = get_streak(hid)
            icon   = "✅" if done else "⬜"
            s_text = f"  🔥{streak}" if streak > 1 else ""
            rows.append([InlineKeyboardButton(f"{icon} {name}{s_text}", callback_data=f"habit_{hid}")])
    rows.append([InlineKeyboardButton("➕ Добавить привычку", callback_data="habit_add")])
    return InlineKeyboardMarkup(rows)


# ════════════════════════════════════
#  КОМАНДЫ
# ════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_setting("chat_id", str(update.effective_chat.id))
    await update.message.reply_text(
        "👋 *Привет! Я твой личный планировщик.*\n\n"

        "📅 *Автоматика каждый день:*\n"
        "• 10:00 — запрашиваю задачи на день\n"
        "• 12:00 — напоминаю про таблетки 💊\n"
        "• 18:00 — спрашиваю что перенести на завтра\n"
        "• Пн/Ср/Чт 9:00 — напоминаю про зал 💪\n"
        "• Вс 20:00 — обзор недели 📊\n\n"

        "📋 *Задачи:*\n"
        "• Нажми ⬜ — задача выполнена и исчезает\n"
        "• Добавь `!` перед задачей → станет 🔴 срочной\n"
        "• ✏️ изменить / 🗑 удалить / ➡️ на завтра — кнопки снизу\n\n"

        "💪 *Привычки:*\n"
        "• Ежедневный чеклист · счётчик 🔥 streak\n\n"

        "📓 *Заметки:*\n"
        "• Нажми *📓 Заметки* → напиши текст → сохранится\n"
        "• Нажми ещё раз — увидишь все заметки\n\n"

        "🔔 *Напоминания:*\n"
        "• Разовое напоминание на любую дату и время\n\n"

        "📊 *Обзор недели:*\n"
        "• % выполненных задач · что переносилось чаще всего\n\n"

        "_Используй кнопки меню внизу_ 👇",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = today_str()
    kb = build_task_kb(today)
    if not kb:
        await update.message.reply_text("🎉 На сегодня активных задач нет — всё выполнено!")
        return
    await update.message.reply_text(
        f"📋 *Задачи на {today}:*\n"
        "_⬜ нажми — выполнено и исчезает · 🔴 = срочная_",
        reply_markup=kb, parse_mode="Markdown"
    )

async def cmd_newtasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_state["mode"] = STATE_WAITING_TASKS
    await update.message.reply_text(
        "📝 *Пришли список задач — каждая с новой строки.*\n\n"
        "Добавь `!` в начало для срочных 🔴:\n"
        "`! Срочно позвонить клиенту`\n"
        "`Обычная задача`",
        parse_mode="Markdown"
    )

async def cmd_habits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = today_str()
    await update.message.reply_text(
        f"💪 *Привычки на {today}:*\n_🔥 = дней подряд · нажми чтобы отметить_",
        reply_markup=build_habit_kb(today), parse_mode="Markdown"
    )

async def cmd_add_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Если уже есть заметки — показываем их, иначе входим в режим добавления
    row = db_fetchone("SELECT COUNT(*) FROM notes")
    count = row[0] if row else 0

    if count > 0 and bot_state["mode"] != STATE_ADDING_NOTE:
        # Показываем список + предлагаем добавить
        notes = db_fetchall("SELECT text, created_at FROM notes ORDER BY id DESC LIMIT 15")
        lines = [f"📌 _{n[1]}_\n{n[0]}" for n in notes]
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Добавить заметку", callback_data="note_add")]])
        await update.message.reply_text(
            "*Твои заметки:*\n\n" + "\n\n─────\n\n".join(lines),
            reply_markup=kb, parse_mode="Markdown"
        )
    else:
        bot_state["mode"] = STATE_ADDING_NOTE
        await update.message.reply_text("📓 Напиши заметку — сохраню с датой и временем:")

async def cmd_done_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_fetchall("""
        SELECT text, completed_at, date FROM tasks
        WHERE done=1 ORDER BY completed_at DESC NULLS LAST LIMIT 30
    """)
    if not rows:
        await update.message.reply_text("Выполненных задач пока нет.")
        return
    lines = []
    for text, completed_at, date in rows:
        when = completed_at if completed_at else date
        lines.append(f"✅ {text}\n   _{when}_")
    await update.message.reply_text(
        "*Выполненные задачи (последние 30):*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown"
    )

async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_weekly_review(context.application, update=update)

async def cmd_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_state["mode"] = STATE_WAITING_REM_TEXT
    await update.message.reply_text("🔔 Напиши текст напоминания:")

async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_fetchall("SELECT text, remind_at FROM reminders WHERE sent=0 ORDER BY remind_at")
    if not rows:
        await update.message.reply_text("Нет предстоящих напоминаний.")
        return
    lines = [f"🔔 *{r[0]}*\n   🕐 {r[1][:16].replace('T', ' ')} (МСК)" for r in rows]
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


# ════════════════════════════════════
#  CALLBACK HANDLER
# ════════════════════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    today = today_str()

    # Отметить задачу выполненной
    if data.startswith("done_"):
        tid = int(data.split("_")[1])
        now_str = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
        row = db_fetchone("SELECT date FROM tasks WHERE id=%s", (tid,))
        if row:
            db_execute("UPDATE tasks SET done=1, completed_at=%s WHERE id=%s", (now_str, tid))
            date = row[0]
        await query.answer("✅ Выполнено!")
        kb = build_task_kb(date)
        if kb:
            await query.edit_message_reply_markup(reply_markup=kb)
        else:
            await query.edit_message_text("🎉 Все задачи на сегодня выполнены!")
        return

    # Режим действия
    if data in ("act_edit", "act_delete", "act_postpone"):
        action = data.split("_")[1]
        titles = {
            "edit":     "✏️ Выбери задачу для изменения:",
            "delete":   "🗑 Выбери задачу для удаления:",
            "postpone": "➡️ Выбери задачу для переноса на завтра:"
        }
        await query.answer()
        await query.edit_message_text(titles[action], reply_markup=build_select_kb(today, action))
        return

    # Отмена
    if data == "act_cancel":
        await query.answer()
        kb = build_task_kb(today)
        if kb:
            await query.edit_message_text(
                f"📋 *Задачи на {today}:*\n_⬜ нажми — выполнено и исчезает · 🔴 = срочная_",
                reply_markup=kb, parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("Задач нет.")
        return

    # Удалить
    if data.startswith("delete_"):
        tid = int(data.split("_")[1])
        db_execute("DELETE FROM tasks WHERE id=%s", (tid,))
        await query.answer("🗑 Удалено")
        kb = build_task_kb(today)
        if kb:
            await query.edit_message_text(f"📋 *Задачи на {today}:*", reply_markup=kb, parse_mode="Markdown")
        else:
            await query.edit_message_text("Задач нет.")
        return

    # Перенести на завтра
    if data.startswith("postpone_"):
        tid      = int(data.split("_")[1])
        tomorrow = tomorrow_str()
        row = db_fetchone("SELECT text, original_date, priority FROM tasks WHERE id=%s", (tid,))
        if row:
            db_execute("DELETE FROM tasks WHERE id=%s", (tid,))
            db_execute(
                "INSERT INTO tasks (text, priority, date, original_date) VALUES (%s,%s,%s,%s)",
                (row[0], row[2], tomorrow, row[1] or today)
            )
        await query.answer("➡️ Перенесено на завтра")
        kb = build_task_kb(today)
        if kb:
            await query.edit_message_text(f"📋 *Задачи на {today}:*", reply_markup=kb, parse_mode="Markdown")
        else:
            await query.edit_message_text("На сегодня задач больше нет.")
        return

    # Выбрать задачу для редактирования
    if data.startswith("edit_"):
        tid = int(data.split("_")[1])
        row = db_fetchone("SELECT text FROM tasks WHERE id=%s", (tid,))
        if row:
            bot_state["mode"] = STATE_EDITING_TASK
            bot_state["temp"]["edit_id"] = tid
            await query.answer()
            await query.edit_message_text(
                f"✏️ Редактируем:\n`{row[0]}`\n\n"
                "Напиши новый текст.\nДобавь `!` в начало для срочной 🔴:",
                parse_mode="Markdown"
            )
        return

    # Привычки
    if data.startswith("habit_") and data != "habit_add":
        hid  = int(data.split("_")[1])
        done = is_habit_done(hid, today)
        if done:
            db_execute("DELETE FROM habit_log WHERE habit_id=%s AND date=%s", (hid, today))
            await query.answer("↩️ Отменено")
        else:
            db_execute("INSERT INTO habit_log (habit_id, date) VALUES (%s,%s) ON CONFLICT DO NOTHING", (hid, today))
            streak = get_streak(hid)
            await query.answer("✅" + (f" 🔥{streak} дней подряд!" if streak > 1 else " Выполнено!"))
        await query.edit_message_reply_markup(reply_markup=build_habit_kb(today))
        return

    if data == "habit_add":
        bot_state["mode"] = STATE_ADDING_HABIT
        await query.answer()
        await query.edit_message_text("💪 Напиши название новой привычки:")
        return

    if data == "note_add":
        bot_state["mode"] = STATE_ADDING_NOTE
        await query.answer()
        await query.edit_message_text("📓 Напиши заметку — сохраню с датой и временем:")
        return

    await query.answer()


# ════════════════════════════════════
#  ОБРАБОТЧИК ТЕКСТА
# ════════════════════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    mode = bot_state["mode"]
    set_setting("chat_id", str(update.effective_chat.id))

    # Кнопки меню
    menu_map = {
        "📋 Задачи":         cmd_tasks,
        "➕ Новые задачи":   cmd_newtasks,
        "✅ Выполненные":    cmd_done_tasks,
        "💪 Привычки":       cmd_habits,
        "📓 Заметки":        cmd_add_note,
        "🔔 Напоминание":    cmd_reminder,
        "📆 Напоминания":    cmd_reminders,
        "📊 Обзор недели":   cmd_weekly,
    }
    if text in menu_map:
        await menu_map[text](update, context)
        return

    # Добавление задач
    if mode == STATE_WAITING_TASKS:
        today = today_str()
        tasks = []
        for line in text.split("\n"):
            clean = re.sub(r'^[\d]+[.)]\s*|^[-–•*]\s*', '', line.strip())
            if clean:
                priority  = 1 if clean.startswith("!") else 0
                task_text = clean.lstrip("! ").strip()
                if task_text:
                    tasks.append((task_text, priority))

        if not tasks:
            await update.message.reply_text("Не смог разобрать. Каждая задача — с новой строки.")
            return

        for t, p in tasks:
            db_execute(
                "INSERT INTO tasks (text, priority, date, original_date) VALUES (%s,%s,%s,%s)",
                (t, p, today, today)
            )

        bot_state["mode"] = STATE_IDLE
        kb = build_task_kb(today)
        await update.message.reply_text(
            f"✅ Добавил *{len(tasks)}* задач на сегодня!\n"
            "_🔴 = срочная · ⬜ = обычная · нажми чтобы отметить_",
            reply_markup=kb, parse_mode="Markdown"
        )

    # Редактирование задачи
    elif mode == STATE_EDITING_TASK:
        tid      = bot_state["temp"].get("edit_id")
        priority = 1 if text.startswith("!") else 0
        new_text = text.lstrip("! ").strip()
        if tid and new_text:
            db_execute("UPDATE tasks SET text=%s, priority=%s WHERE id=%s", (new_text, priority, tid))
        bot_state.update({"mode": STATE_IDLE, "temp": {}})
        kb = build_task_kb(today_str())
        await update.message.reply_text("✅ Задача обновлена!", reply_markup=kb)

    # Сохранение заметки
    elif mode == STATE_ADDING_NOTE:
        now_str = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
        db_execute("INSERT INTO notes (text, created_at) VALUES (%s,%s)", (text, now_str))
        bot_state["mode"] = STATE_IDLE
        await update.message.reply_text(
            "📓 Заметка сохранена!\n\nНажми *📓 Заметки* чтобы посмотреть все.",
            parse_mode="Markdown"
        )

    # Добавление привычки
    elif mode == STATE_ADDING_HABIT:
        db_execute("INSERT INTO habits (name) VALUES (%s)", (text,))
        bot_state["mode"] = STATE_IDLE
        await update.message.reply_text(
            f"✅ Привычка добавлена: *{text}*",
            reply_markup=build_habit_kb(today_str()), parse_mode="Markdown"
        )

    # Перенос задач вечером
    elif mode == STATE_WAITING_CARRYOVER:
        today    = today_str()
        tomorrow = tomorrow_str()
        low = text.lower()

        undone = db_fetchall(
            "SELECT id, text, original_date, priority FROM tasks WHERE date=%s AND done=0", (today,)
        )

        if not undone or low in ["нет", "ничего", "-", "no", "0"]:
            bot_state["mode"] = STATE_IDLE
            await update.message.reply_text("Хорошо, ничего не переносим. Хорошего вечера! 🌙")
            return

        to_carry = undone if low in ["все", "всё", "да", "all", "yes"] else \
                   [undone[i] for i in [int(n)-1 for n in re.findall(r'\d+', text)] if 0 <= i < len(undone)]

        if to_carry:
            for _, t, orig, prio in to_carry:
                db_execute(
                    "INSERT INTO tasks (text, priority, date, original_date) VALUES (%s,%s,%s,%s)",
                    (t, prio, tomorrow, orig or today)
                )
            names = "\n".join(f"• {t[1]}" for t in to_carry)
            await update.message.reply_text(
                f"📅 Перенёс на завтра ({tomorrow}):\n{names}\n\nХорошего вечера! 🌙"
            )
        else:
            await update.message.reply_text("Ничего не перенесено. Хорошего вечера! 🌙")
        bot_state["mode"] = STATE_IDLE

    # Напоминание: текст
    elif mode == STATE_WAITING_REM_TEXT:
        bot_state["temp"]["rem_text"] = text
        bot_state["mode"] = STATE_WAITING_REM_DATE
        await update.message.reply_text(
            "📅 Когда напомнить?\nФормат: `ДД.ММ.ГГГГ ЧЧ:ММ`\nПример: `25.04.2025 14:30`",
            parse_mode="Markdown"
        )

    # Напоминание: дата
    elif mode == STATE_WAITING_REM_DATE:
        try:
            dt = MOSCOW_TZ.localize(datetime.strptime(text, "%d.%m.%Y %H:%M"))
            rem_text = bot_state["temp"].get("rem_text", "")
            db_execute("INSERT INTO reminders (text, remind_at) VALUES (%s,%s)", (rem_text, dt.isoformat()))
            bot_state.update({"mode": STATE_IDLE, "temp": {}})
            await update.message.reply_text(f"✅ Напоминание сохранено!\n📝 {rem_text}\n🕐 {text} (МСК)")
        except ValueError:
            await update.message.reply_text(
                "❌ Неверный формат. Попробуй: `ДД.ММ.ГГГГ ЧЧ:ММ`", parse_mode="Markdown"
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
    carried = [t for t in get_tasks(today) if t[4] and t[4] != today]
    msg = "🌅 *Доброе утро!* Пришли список задач на сегодня.\nДобавь `!` перед задачей для срочных 🔴"
    if carried:
        lines = "\n".join(f"• {t[1]}  _(с {t[4]})_" for t in carried)
        msg += f"\n\nПереносы со вчера:\n{lines}"
    await app.bot.send_message(chat_id=int(chat_id), text=msg, parse_mode="Markdown")

async def job_pills(app):
    chat_id = get_setting("chat_id")
    if not chat_id:
        return
    await app.bot.send_message(chat_id=int(chat_id), text="💊 Время выпить таблетки!")

async def job_gym(app):
    chat_id = get_setting("chat_id")
    if not chat_id:
        return
    await app.bot.send_message(
        chat_id=int(chat_id),
        text="💪 Сегодня день зала! Не пропусти тренировку 🏋️"
    )

async def job_evening(app):
    chat_id = get_setting("chat_id")
    if not chat_id:
        return
    today  = today_str()
    undone = get_tasks(today, done=0)
    if not undone:
        await app.bot.send_message(chat_id=int(chat_id), text="🌆 Отличный день — все задачи выполнены! 🎉")
        return
    bot_state["mode"] = STATE_WAITING_CARRYOVER
    lines = "\n".join(f"{i+1}. {t[1]}" for i, t in enumerate(undone))
    await app.bot.send_message(
        chat_id=int(chat_id),
        text=(
            f"🌆 *Добрый вечер!* Невыполненные задачи:\n\n{lines}\n\n"
            "Что перенести на завтра?\n"
            "• *все* — перенести всё\n"
            "• *1, 3* — конкретные номера\n"
            "• *нет* — ничего не переносить"
        ),
        parse_mode="Markdown"
    )

async def send_weekly_review(app, update=None):
    chat_id = get_setting("chat_id")
    today   = datetime.now(MOSCOW_TZ)
    total_done = total_tasks = 0

    for i in range(7):
        d = (today - timedelta(days=i)).strftime("%d.%m.%Y")
        total_done  += db_fetchone("SELECT COUNT(*) FROM tasks WHERE date=%s AND done=1", (d,))[0]
        total_tasks += db_fetchone("SELECT COUNT(*) FROM tasks WHERE date=%s", (d,))[0]
    week_ago = (today - timedelta(days=7)).strftime("%d.%m.%Y")
    carried  = db_fetchall("""
        SELECT text, COUNT(*) as cnt FROM tasks
        WHERE original_date != date AND original_date IS NOT NULL AND date >= %s
        GROUP BY text ORDER BY cnt DESC LIMIT 3
    """, (week_ago,))

    pct   = int(total_done / total_tasks * 100) if total_tasks else 0
    emoji = "🔥" if pct >= 80 else "👍" if pct >= 50 else "💪"
    msg   = f"📊 *Обзор недели*\n\n{emoji} Выполнено: *{total_done} из {total_tasks}* задач ({pct}%)\n"
    if carried:
        msg += "\n🔄 *Чаще всего переносилось:*\n" + "\n".join(f"• {t[0]}  ×{t[1]}" for t in carried)
    msg += {100: "\n\n🏆 Идеальная неделя!", 80: "\n\nОтличная неделя!"}.get(
        pct if pct == 100 else (80 if pct >= 80 else (50 if pct >= 50 else 0)),
        "\n\nСложная неделя. Начнём новую! 🌅" if pct < 50 else "\n\nНеплохо, но есть куда расти 💪"
    )

    if update:
        await update.message.reply_text(msg, parse_mode="Markdown")
    elif chat_id:
        await app.bot.send_message(chat_id=int(chat_id), text=msg, parse_mode="Markdown")

async def job_reminders(app):
    chat_id = get_setting("chat_id")
    if not chat_id:
        return
    now = datetime.now(MOSCOW_TZ)
    rows = db_fetchall("SELECT id, text, remind_at FROM reminders WHERE sent=0")
    for rid, text, rat in rows:
        try:
            dt = datetime.fromisoformat(rat)
            if dt.tzinfo is None:
                dt = MOSCOW_TZ.localize(dt)
            if now >= dt:
                await app.bot.send_message(
                    chat_id=int(chat_id),
                    text=f"🔔 *Напоминание:* {text}", parse_mode="Markdown"
                )
                db_execute("UPDATE reminders SET sent=1 WHERE id=%s", (rid,))
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
    app.add_handler(CommandHandler("done",      cmd_done_tasks))
    app.add_handler(CommandHandler("habits",    cmd_habits))
    app.add_handler(CommandHandler("notes",     cmd_add_note))
    app.add_handler(CommandHandler("weekly",    cmd_weekly))
    app.add_handler(CommandHandler("reminder",  cmd_reminder))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    scheduler.add_job(job_morning,        "cron", hour=10, minute=0,                            args=[app])
    scheduler.add_job(job_pills,          "cron", hour=12, minute=0,                            args=[app])
    scheduler.add_job(job_evening,        "cron", hour=18, minute=0,                            args=[app])
    scheduler.add_job(job_gym,            "cron", day_of_week="mon,wed,thu", hour=9, minute=0,  args=[app])
    scheduler.add_job(send_weekly_review, "cron", day_of_week="sun",         hour=20, minute=0, args=[app])
    scheduler.add_job(job_reminders,      "interval", minutes=1,                                args=[app])
    scheduler.start()

    async def post_init(application):
        await application.bot.set_my_commands([
            ("start",     "👋 Главное меню и справка"),
            ("tasks",     "📋 Задачи на сегодня"),
            ("newtasks",  "➕ Добавить новые задачи"),
            ("done",      "✅ Выполненные задачи"),
            ("habits",    "💪 Привычки и streak"),
            ("notes",     "📓 Заметки"),
            ("weekly",    "📊 Обзор недели"),
            ("reminder",  "🔔 Добавить напоминание"),
            ("reminders", "📆 Все напоминания"),
        ])

    app.post_init = post_init

    logging.info("✅ Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
