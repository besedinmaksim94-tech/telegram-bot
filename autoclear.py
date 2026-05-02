import asyncio
import logging
import os
import time
import random
import pytz
import aiosqlite

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, JOIN_TRANSITION
from apscheduler.schedulers.asyncio import AsyncIOScheduler

API_TOKEN = os.getenv("API_TOKEN")

DEFAULT_MIN_MESSAGES = 50
COOLDOWN = 300
GRACE_HOURS = 12

TIMEZONE = pytz.timezone("Europe/Moscow")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

START_TIME = time.time()

# ========= УРОВНИ =========

LEVELS = [
    (0,    "🥚 Новичок"),
    (50,   "🐣 Птенец"),
    (150,  "🐥 Активист"),
    (300,  "🌟 Завсегдатай"),
    (600,  "💫 Ветеран"),
    (1000, "🔥 Легенда"),
    (2000, "👑 Король чата"),
]

def get_level(total: int) -> tuple[str, int, int | None]:
    current_thresh, current_name = LEVELS[0]
    for threshold, name in LEVELS:
        if total >= threshold:
            current_thresh, current_name = threshold, name
    idx = next(i for i, (t, _) in enumerate(LEVELS) if t == current_thresh)
    next_thresh = LEVELS[idx + 1][0] if idx + 1 < len(LEVELS) else None
    return current_name, current_thresh, next_thresh


# ========= ДОСТИЖЕНИЯ =========

ACHIEVEMENTS = {
    "first_100":  ("📬", "Первая сотня",   "Написал 100 сообщений"),
    "first_500":  ("📮", "500 сообщений",  "Написал 500 сообщений"),
    "first_1000": ("🏅", "Тысячник",       "Написал 1000 сообщений"),
    "top1_week":  ("🥇", "Лидер недели",   "Первое место в топе недели"),
    "gamer":      ("🎮", "Игроман",        "Сыграл 10 раз в мини-игры"),
    "lucky":      ("🍀", "Счастливчик",    "Выпало максимальное значение в игре"),
    "survivor":   ("🛡️", "Выживший",       "Пережил еженедельную чистку"),
}


# ========= DB =========

async def init_db():
    async with aiosqlite.connect("db.sqlite") as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id   INTEGER PRIMARY KEY,
                title     TEXT,
                joined_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS messages (
                chat_id   INTEGER,
                user_id   INTEGER,
                username  TEXT,
                full_name TEXT,
                count     INTEGER DEFAULT 0,
                total     INTEGER DEFAULT 0,
                PRIMARY KEY(chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS whitelist (
                chat_id  INTEGER,
                user_id  INTEGER,
                added_by INTEGER,
                added_at INTEGER,
                PRIMARY KEY(chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER,
                key     TEXT,
                value   TEXT,
                PRIMARY KEY(chat_id, key)
            );
            CREATE TABLE IF NOT EXISTS cooldown (
                chat_id INTEGER,
                user_id INTEGER,
                type    TEXT,
                last    INTEGER,
                PRIMARY KEY(chat_id, user_id, type)
            );
            CREATE TABLE IF NOT EXISTS kick_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id   INTEGER,
                user_id   INTEGER,
                username  TEXT,
                full_name TEXT,
                msg_count INTEGER,
                kicked_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS achievements (
                chat_id     INTEGER,
                user_id     INTEGER,
                achievement TEXT,
                unlocked_at INTEGER,
                PRIMARY KEY(chat_id, user_id, achievement)
            );
            CREATE TABLE IF NOT EXISTS game_stats (
                chat_id      INTEGER,
                user_id      INTEGER,
                games_played INTEGER DEFAULT 0,
                max_roll     INTEGER DEFAULT 0,
                PRIMARY KEY(chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS weekly_snapshot (
                chat_id   INTEGER,
                user_id   INTEGER,
                username  TEXT,
                full_name TEXT,
                count     INTEGER DEFAULT 0,
                PRIMARY KEY(chat_id, user_id)
            );
        """)
        await db.commit()


# ========= HELPERS =========

async def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False


def user_label(username: str, full_name: str, user_id: int) -> str:
    if username:
        return f"@{username}"
    if full_name:
        return full_name
    return f"id{user_id}"


async def register_chat(chat_id: int, title: str):
    async with aiosqlite.connect("db.sqlite") as db:
        await db.execute(
            "INSERT OR IGNORE INTO chats VALUES(?,?,?)",
            (chat_id, title, int(time.time())),
        )
        await db.commit()


async def get_all_chats() -> list[int]:
    async with aiosqlite.connect("db.sqlite") as db:
        async with db.execute("SELECT chat_id FROM chats") as cur:
            return [r[0] for r in await cur.fetchall()]


async def add_message(chat_id: int, user: types.User):
    async with aiosqlite.connect("db.sqlite") as db:
        await db.execute("""
            INSERT INTO messages(chat_id, user_id, username, full_name, count, total)
            VALUES(?,?,?,?,1,1)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                count     = count + 1,
                total     = total + 1,
                username  = excluded.username,
                full_name = excluded.full_name
        """, (chat_id, user.id, user.username or "", user.full_name))
        await db.commit()
    await check_message_achievements(chat_id, user)


async def get_stats(chat_id: int):
    async with aiosqlite.connect("db.sqlite") as db:
        async with db.execute(
            "SELECT user_id, username, full_name, count, total FROM messages WHERE chat_id=?",
            (chat_id,),
        ) as cur:
            return await cur.fetchall()


async def reset_weekly_stats(chat_id: int):
    async with aiosqlite.connect("db.sqlite") as db:
        await db.execute("DELETE FROM weekly_snapshot WHERE chat_id=?", (chat_id,))
        await db.execute("""
            INSERT INTO weekly_snapshot
            SELECT chat_id, user_id, username, full_name, count
            FROM messages WHERE chat_id=?
        """, (chat_id,))
        await db.execute("UPDATE messages SET count=0 WHERE chat_id=?", (chat_id,))
        await db.commit()


async def get_min(chat_id: int) -> int:
    async with aiosqlite.connect("db.sqlite") as db:
        async with db.execute(
            "SELECT value FROM settings WHERE chat_id=? AND key='min'", (chat_id,)
        ) as cur:
            r = await cur.fetchone()
            return int(r[0]) if r else DEFAULT_MIN_MESSAGES


async def set_min(chat_id: int, val: int):
    async with aiosqlite.connect("db.sqlite") as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings VALUES(?,'min',?)", (chat_id, str(val))
        )
        await db.commit()


async def is_whitelisted(chat_id: int, uid: int) -> bool:
    async with aiosqlite.connect("db.sqlite") as db:
        async with db.execute(
            "SELECT 1 FROM whitelist WHERE chat_id=? AND user_id=?", (chat_id, uid)
        ) as cur:
            return await cur.fetchone() is not None


async def add_whitelist(chat_id: int, uid: int, added_by: int):
    async with aiosqlite.connect("db.sqlite") as db:
        await db.execute(
            "INSERT OR IGNORE INTO whitelist VALUES(?,?,?,?)",
            (chat_id, uid, added_by, int(time.time())),
        )
        await db.commit()


async def remove_whitelist(chat_id: int, uid: int):
    async with aiosqlite.connect("db.sqlite") as db:
        await db.execute(
            "DELETE FROM whitelist WHERE chat_id=? AND user_id=?", (chat_id, uid)
        )
        await db.commit()


async def log_kick(chat_id, user_id, username, full_name, count):
    async with aiosqlite.connect("db.sqlite") as db:
        await db.execute(
            "INSERT INTO kick_log(chat_id,user_id,username,full_name,msg_count,kicked_at) VALUES(?,?,?,?,?,?)",
            (chat_id, user_id, username, full_name, count, int(time.time())),
        )
        await db.commit()


# ========= ДОСТИЖЕНИЯ =========

async def unlock_achievement(chat_id: int, uid: int, key: str, username: str, full_name: str):
    async with aiosqlite.connect("db.sqlite") as db:
        async with db.execute(
            "SELECT 1 FROM achievements WHERE chat_id=? AND user_id=? AND achievement=?",
            (chat_id, uid, key),
        ) as cur:
            if await cur.fetchone():
                return False
        await db.execute(
            "INSERT INTO achievements VALUES(?,?,?,?)",
            (chat_id, uid, key, int(time.time())),
        )
        await db.commit()
    emoji, name, _ = ACHIEVEMENTS[key]
    label = user_label(username, full_name, uid)
    try:
        await bot.send_message(
            chat_id,
            f"🏆 <b>{label}</b> получил достижение {emoji} <b>{name}</b>!",
            parse_mode="HTML",
        )
    except Exception:
        pass
    return True


async def check_message_achievements(chat_id: int, user: types.User):
    async with aiosqlite.connect("db.sqlite") as db:
        async with db.execute(
            "SELECT total FROM messages WHERE chat_id=? AND user_id=?",
            (chat_id, user.id),
        ) as cur:
            r = await cur.fetchone()
    if not r:
        return
    total = r[0]
    for key, threshold in [("first_100", 100), ("first_500", 500), ("first_1000", 1000)]:
        if total == threshold:
            await unlock_achievement(chat_id, user.id, key, user.username or "", user.full_name)


async def check_game_achievements(chat_id: int, user: types.User, roll: int, max_possible: int):
    async with aiosqlite.connect("db.sqlite") as db:
        await db.execute("""
            INSERT INTO game_stats(chat_id, user_id, games_played, max_roll) VALUES(?,?,1,?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                games_played = games_played + 1,
                max_roll     = MAX(max_roll, ?)
        """, (chat_id, user.id, roll, roll))
        await db.commit()
        async with db.execute(
            "SELECT games_played FROM game_stats WHERE chat_id=? AND user_id=?",
            (chat_id, user.id),
        ) as cur:
            r = await cur.fetchone()
    if r and r[0] == 10:
        await unlock_achievement(chat_id, user.id, "gamer", user.username or "", user.full_name)
    if roll == max_possible:
        await unlock_achievement(chat_id, user.id, "lucky", user.username or "", user.full_name)


# ========= БОТ ДОБАВЛЕН В ЧАТ =========

@dp.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def bot_added(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return
    await register_chat(chat.id, chat.title or "")
    schedule_chat_clean(chat.id)
    logger.info(f"Added to chat {chat.id} ({chat.title})")
    try:
        await bot.send_message(
            chat.id,
            "👋 <b>Привет!</b> Я бот авто-очистки чата.\n\n"
            "📊 Считаю сообщения, выдаю уровни и достижения\n"
            "🧹 Кикаю неактивных каждое воскресенье 18:00 МСК\n\n"
            f"Минимум сообщений: <b>{DEFAULT_MIN_MESSAGES}</b> в неделю\n"
            "Изменить: /set_min 30\n\n"
            "/help — все команды",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ========= КОМАНДЫ =========

@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return await m.answer(
            "👋 Привет! Добавь меня в группу и я начну работать.\n\n"
            "Я слежу за активностью, выдаю уровни, достижения и кикаю неактивных."
        )
    await register_chat(m.chat.id, m.chat.title or "")
    await m.answer(
        f"👋 <b>Привет, {m.from_user.full_name}!</b>\n/help — список команд",
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def cmd_help(m: types.Message):
    await m.answer(
        "<b>📖 Команды бота</b>\n\n"
        "<b>⚙️ Только для админов:</b>\n"
        "/set_min 50 — минимум сообщений в неделю\n"
        "/force — принудительная чистка\n"
        "/activity — кто под угрозой\n"
        "/whitelist add|remove|list\n"
        "/kicklog — лог киков\n\n"
        "<b>👀 Для всех:</b>\n"
        "/top — топ активных\n"
        "/mystats — моя статистика\n"
        "/profile — профиль с уровнем\n"
        "/achievements — мои достижения\n\n"
        "<b>🎮 Игры (раз в 5 мин):</b>\n"
        ".кур 🚬  .пиво 🍺  .вино 🍷\n"
        ".дайс 🎲  .монета 🪙  .рулетка 🔫\n\n"
        "<b>ℹ️</b> Чистка каждое вс 18:00 МСК",
        parse_mode="HTML",
    )


@dp.message(Command("set_min"))
async def cmd_set_min(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    if not await is_admin(m.chat.id, m.from_user.id):
        return
    parts = m.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await m.answer("❌ Использование: /set_min <число>")
    val = int(parts[1])
    await set_min(m.chat.id, val)
    await m.answer(f"✅ Минимум сообщений = <b>{val}</b>", parse_mode="HTML")


@dp.message(Command("top"))
async def cmd_top(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    data = sorted(await get_stats(m.chat.id), key=lambda x: x[3], reverse=True)[:10]
    if not data:
        return await m.answer("📭 Статистика пуста")
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, uname, fname, count, total) in enumerate(data):
        prefix = medals[i] if i < 3 else f"{i+1}."
        label = user_label(uname, fname, uid)
        level_name, _, _ = get_level(total)
        lines.append(f"{prefix} {label} — <b>{count}</b> {level_name}")
    minv = await get_min(m.chat.id)
    await m.answer(
        f"🏆 <b>ТОП активности</b> (мин. {minv}):\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


@dp.message(Command("mystats"))
async def cmd_mystats(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    async with aiosqlite.connect("db.sqlite") as db:
        async with db.execute(
            "SELECT count, total FROM messages WHERE chat_id=? AND user_id=?",
            (m.chat.id, m.from_user.id),
        ) as cur:
            r = await cur.fetchone()
    count, total = (r[0], r[1]) if r else (0, 0)
    minv = await get_min(m.chat.id)
    status = "✅ в безопасности" if count >= minv else f"⚠️ нужно ещё {minv - count}"
    level_name, _, next_thresh = get_level(total)
    progress = f"\nДо следующего уровня: {next_thresh - total} сообщ." if next_thresh else ""
    await m.answer(
        f"📊 <b>Твоя статистика</b>\n\n"
        f"За неделю: <b>{count}</b> (мин. {minv}) — {status}\n"
        f"Всего: <b>{total}</b>\n"
        f"Уровень: {level_name}{progress}",
        parse_mode="HTML",
    )


@dp.message(Command("profile"))
async def cmd_profile(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    if m.reply_to_message:
        target = m.reply_to_message.from_user
        uid, uname, fname = target.id, target.username or "", target.full_name
    else:
        uid, uname, fname = m.from_user.id, m.from_user.username or "", m.from_user.full_name

    async with aiosqlite.connect("db.sqlite") as db:
        async with db.execute(
            "SELECT count, total FROM messages WHERE chat_id=? AND user_id=?",
            (m.chat.id, uid),
        ) as cur:
            r = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM achievements WHERE chat_id=? AND user_id=?",
            (m.chat.id, uid),
        ) as cur:
            ach_count = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT games_played, max_roll FROM game_stats WHERE chat_id=? AND user_id=?",
            (m.chat.id, uid),
        ) as cur:
            gs = await cur.fetchone()

    count, total = (r[0], r[1]) if r else (0, 0)
    games_played = gs[0] if gs else 0
    max_roll = gs[1] if gs else 0
    wl = await is_whitelisted(m.chat.id, uid)
    level_name, _, next_thresh = get_level(total)
    label = user_label(uname, fname, uid)

    bar_max = next_thresh or max(total, 1)
    bar_fill = min(10, int(total / bar_max * 10))
    bar = "█" * bar_fill + "░" * (10 - bar_fill)

    await m.answer(
        f"👤 <b>Профиль: {label}</b>\n\n"
        f"Уровень: {level_name}\n"
        f"[{bar}] {total}/{next_thresh or '∞'}\n\n"
        f"📨 Всего сообщений: <b>{total}</b>\n"
        f"📅 За эту неделю: <b>{count}</b>\n"
        f"🎮 Игр сыграно: <b>{games_played}</b>\n"
        f"🎯 Макс. бросок: <b>{max_roll}</b>\n"
        f"🏆 Достижений: <b>{ach_count}/{len(ACHIEVEMENTS)}</b>\n"
        f"🛡️ Whitelist: {'да' if wl else 'нет'}",
        parse_mode="HTML",
    )


@dp.message(Command("achievements"))
async def cmd_achievements(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    if m.reply_to_message:
        target = m.reply_to_message.from_user
        uid, fname = target.id, target.full_name
    else:
        uid, fname = m.from_user.id, m.from_user.full_name

    async with aiosqlite.connect("db.sqlite") as db:
        async with db.execute(
            "SELECT achievement FROM achievements WHERE chat_id=? AND user_id=?",
            (m.chat.id, uid),
        ) as cur:
            unlocked = {r[0] for r in await cur.fetchall()}

    lines = []
    for key, (emoji, name, desc) in ACHIEVEMENTS.items():
        if key in unlocked:
            lines.append(f"{emoji} <b>{name}</b> — {desc}")
        else:
            lines.append(f"🔒 <i>{name}</i>")

    await m.answer(
        f"🏆 <b>Достижения {fname}:</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


@dp.message(Command("activity"))
async def cmd_activity(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    if not await is_admin(m.chat.id, m.from_user.id):
        return
    minv = await get_min(m.chat.id)
    data = await get_stats(m.chat.id)
    at_risk = [(uid, uname, fname, c) for uid, uname, fname, c, _ in data if c < minv]
    at_risk.sort(key=lambda x: x[3])
    if not at_risk:
        return await m.answer(f"✅ Все активны (минимум {minv})")
    lines = [f"• {user_label(u,f,uid)} — {c}/{minv}" for uid,u,f,c in at_risk[:30]]
    await m.answer(
        f"⚠️ <b>Под угрозой кика ({len(at_risk)} чел.):</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


@dp.message(Command("whitelist"))
async def cmd_whitelist(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    if not await is_admin(m.chat.id, m.from_user.id):
        return
    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("❌ /whitelist add|remove|list")
    action = parts[1].lower()

    if action == "list":
        async with aiosqlite.connect("db.sqlite") as db:
            async with db.execute(
                "SELECT user_id FROM whitelist WHERE chat_id=?", (m.chat.id,)
            ) as cur:
                rows = await cur.fetchall()
        if not rows:
            return await m.answer("📋 Whitelist пуст")
        return await m.answer(
            "📋 <b>Whitelist:</b>\n" + "\n".join(f"• {r[0]}" for r in rows),
            parse_mode="HTML",
        )

    if m.reply_to_message:
        uid = m.reply_to_message.from_user.id
    elif len(parts) >= 3 and parts[2].lstrip("@").isdigit():
        uid = int(parts[2].lstrip("@"))
    else:
        return await m.answer("❌ Ответь на сообщение или укажи числовой ID")

    if action == "add":
        await add_whitelist(m.chat.id, uid, m.from_user.id)
        await m.answer(f"✅ <code>{uid}</code> добавлен", parse_mode="HTML")
    elif action == "remove":
        await remove_whitelist(m.chat.id, uid)
        await m.answer(f"✅ <code>{uid}</code> удалён", parse_mode="HTML")
    else:
        await m.answer("❌ Неизвестное действие: add | remove | list")


@dp.message(Command("kicklog"))
async def cmd_kicklog(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    if not await is_admin(m.chat.id, m.from_user.id):
        return
    async with aiosqlite.connect("db.sqlite") as db:
        async with db.execute(
            "SELECT user_id,username,full_name,msg_count,kicked_at FROM kick_log "
            "WHERE chat_id=? ORDER BY kicked_at DESC LIMIT 20",
            (m.chat.id,),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return await m.answer("📭 Лог пуст")
    lines = []
    for uid, uname, fname, count, ts in rows:
        label = user_label(uname, fname, uid)
        dt = time.strftime("%d.%m %H:%M", time.localtime(ts))
        lines.append(f"• {label} — {count} сообщ. [{dt}]")
    await m.answer("🗑 <b>Последние кики:</b>\n\n" + "\n".join(lines), parse_mode="HTML")


@dp.message(Command("force"))
async def cmd_force(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    if not await is_admin(m.chat.id, m.from_user.id):
        return
    await m.answer("⚙️ Запускаю принудительную чистку...")
    kicked = await do_clean(m.chat.id, force=True)
    await m.answer(f"✅ Готово. Кикнуто: <b>{kicked}</b> чел.", parse_mode="HTML")


# ========= ИГРЫ =========

async def check_cooldown(chat_id: int, uid: int, t: str) -> int:
    async with aiosqlite.connect("db.sqlite") as db:
        async with db.execute(
            "SELECT last FROM cooldown WHERE chat_id=? AND user_id=? AND type=?",
            (chat_id, uid, t),
        ) as cur:
            r = await cur.fetchone()
        now = int(time.time())
        if r and now - r[0] < COOLDOWN:
            return COOLDOWN - (now - r[0])
        await db.execute(
            "INSERT OR REPLACE INTO cooldown VALUES(?,?,?,?)", (chat_id, uid, t, now)
        )
        await db.commit()
        return 0


async def play_game(m: types.Message, gtype: str, maxv: int, verb: str, emoji: str):
    if m.chat.type not in ("group", "supergroup"):
        return
    cd = await check_cooldown(m.chat.id, m.from_user.id, gtype)
    if cd:
        mins, secs = divmod(cd, 60)
        wait = f"{mins}м {secs}с" if mins else f"{secs}с"
        return await m.reply(f"⏳ Подожди ещё {wait}")
    val = random.randint(1, maxv)
    await m.answer(
        f"<b>{m.from_user.full_name}</b> {verb} <b>{val}</b> {emoji}",
        parse_mode="HTML",
    )
    await check_game_achievements(m.chat.id, m.from_user, val, maxv)


@dp.message(F.text == ".кур")
async def game_smoke(m: types.Message):
    await play_game(m, "smoke", 8, "скурил", "🚬")

@dp.message(F.text == ".пиво")
async def game_beer(m: types.Message):
    await play_game(m, "beer", 5, "выпил", "🍺")

@dp.message(F.text == ".вино")
async def game_wine(m: types.Message):
    await play_game(m, "wine", 4, "выпил", "🍷")


@dp.message(F.text == ".дайс")
async def game_dice(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    cd = await check_cooldown(m.chat.id, m.from_user.id, "dice")
    if cd:
        mins, secs = divmod(cd, 60)
        return await m.reply(f"⏳ Подожди {mins}м {secs}с" if mins else f"⏳ Подожди {secs}с")
    val = random.randint(1, 6)
    icons = ["⚀", "⚁", "⚂", "⚃", "⚄", "⚅"]
    comment = " — удача!" if val == 6 else (" — невезение..." if val == 1 else "")
    await m.answer(
        f"🎲 <b>{m.from_user.full_name}</b> бросил кубик: {icons[val-1]} <b>{val}</b>{comment}",
        parse_mode="HTML",
    )
    await check_game_achievements(m.chat.id, m.from_user, val, 6)


@dp.message(F.text == ".монета")
async def game_coin(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    cd = await check_cooldown(m.chat.id, m.from_user.id, "coin")
    if cd:
        return await m.reply(f"⏳ Подожди {cd}с")
    result, val = random.choice([("🪙 Орёл!", 1), ("🌑 Решка!", 0)])
    await m.answer(
        f"<b>{m.from_user.full_name}</b> подбросил монету: <b>{result}</b>",
        parse_mode="HTML",
    )
    await check_game_achievements(m.chat.id, m.from_user, val, 1)


@dp.message(F.text == ".рулетка")
async def game_roulette(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    cd = await check_cooldown(m.chat.id, m.from_user.id, "roulette")
    if cd:
        mins, secs = divmod(cd, 60)
        return await m.reply(f"⏳ Подожди {mins}м {secs}с" if mins else f"⏳ Подожди {secs}с")
    shot = random.randint(1, 6) == 1
    if shot:
        await m.answer(
            f"🔫 <b>{m.from_user.full_name}</b> нажал на курок... 💥 Выстрел! RIP\n"
            f"<i>(мьют на 1 минуту)</i>",
            parse_mode="HTML",
        )
        try:
            until = int(time.time()) + 60
            await bot.restrict_chat_member(
                m.chat.id, m.from_user.id,
                permissions=types.ChatPermissions(can_send_messages=False),
                until_date=until,
            )
        except Exception:
            pass
    else:
        await m.answer(
            f"🔫 <b>{m.from_user.full_name}</b> нажал на курок... <i>*клик*</i> Повезло!",
            parse_mode="HTML",
        )
    await check_game_achievements(m.chat.id, m.from_user, 0 if shot else 1, 1)


# ========= СЧЁТЧИК СООБЩЕНИЙ =========

@dp.message()
async def handler_all(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    if not m.from_user or m.from_user.is_bot:
        return
    await register_chat(m.chat.id, m.chat.title or "")
    await add_message(m.chat.id, m.from_user)


# ========= ЧИСТКА =========

async def do_clean(chat_id: int, force: bool = False) -> int:
    if not force and time.time() - START_TIME < GRACE_HOURS * 3600:
        return 0

    stats = await get_stats(chat_id)
    minv = await get_min(chat_id)

    if not force:
        at_risk = [(uid, uname, fname, c) for uid, uname, fname, c, _ in stats if c < minv]
        if at_risk:
            lines = [f"• {user_label(u,f,uid)} — {c}/{minv}" for uid,u,f,c in at_risk[:30]]
            try:
                await bot.send_message(
                    chat_id,
                    f"⚠️ <b>Завтра в 18:00 пройдёт чистка!</b>\n\n"
                    f"Под угрозой кика ({len(at_risk)} чел.):\n" + "\n".join(lines),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return 0

    kicked = 0
    for uid, uname, fname, count, total in stats:
        if count >= minv:
            await unlock_achievement(chat_id, uid, "survivor", uname, fname)
            continue
        try:
            if await is_admin(chat_id, uid):
                continue
            if await is_whitelisted(chat_id, uid):
                continue
            await bot.ban_chat_member(chat_id, uid)
            await asyncio.sleep(0.5)
            await bot.unban_chat_member(chat_id, uid)
            await log_kick(chat_id, uid, uname, fname, count)
            logger.info(f"[{chat_id}] Kicked {user_label(uname,fname,uid)} — {count}/{minv}")
            kicked += 1
            await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"[{chat_id}] Failed to kick {uid}: {e}")

    top = sorted(stats, key=lambda x: x[3], reverse=True)
    if top:
        uid, uname, fname = top[0][0], top[0][1], top[0][2]
        await unlock_achievement(chat_id, uid, "top1_week", uname, fname)

    await reset_weekly_stats(chat_id)

    if kicked > 0:
        try:
            await bot.send_message(
                chat_id,
                f"🧹 <b>Еженедельная чистка завершена.</b>\nКикнуто: <b>{kicked}</b> чел.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    logger.info(f"[{chat_id}] Clean done. Kicked {kicked}.")
    return kicked


# ========= ШЕДУЛЕР =========

def schedule_chat_clean(chat_id: int):
    job_warn_id = f"warn_{chat_id}"
    job_clean_id = f"clean_{chat_id}"
    if scheduler.get_job(job_warn_id):
        return  # уже запланировано
    scheduler.add_job(
        lambda: asyncio.create_task(do_clean(chat_id, force=False)),
        "cron", day_of_week="sat", hour=18,
        id=job_warn_id,
    )
    scheduler.add_job(
        lambda: asyncio.create_task(do_clean(chat_id, force=True)),
        "cron", day_of_week="sun", hour=18,
        id=job_clean_id,
    )
    logger.info(f"Scheduled clean for chat {chat_id}")


async def setup_scheduler():
    chats = await get_all_chats()
    for chat_id in chats:
        schedule_chat_clean(chat_id)
    scheduler.start()
    logger.info(f"Scheduler started for {len(chats)} chats")


# ========= RUN =========

async def main():
    await init_db()
    await setup_scheduler()
    logger.info("Bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())