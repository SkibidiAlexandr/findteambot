import asyncio, hashlib, hmac, json, os, random, re, sqlite3, time
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           MenuButtonWebApp, Message, WebAppInfo)

TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]  # публичный https-адрес этого сервера
PORT = int(os.getenv("PORT", "8080"))
HERE = os.path.dirname(os.path.abspath(__file__))

GAME_RE = re.compile(r"[a-z0-9_]{1,24}")  # id игры из каталога в index.html
MAX_GAMES = 60

DATA_DIR = os.getenv("DATA_DIR", HERE)  # на Railway сюда монтируется Volume
os.makedirs(DATA_DIR, exist_ok=True)
db = sqlite3.connect(os.path.join(DATA_DIR, "allies.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, name TEXT, age INTEGER,
  bio TEXT, photo TEXT, amin INTEGER DEFAULT 12, amax INTEGER DEFAULT 99);
CREATE TABLE IF NOT EXISTS swipes(
  a INTEGER, b INTEGER, liked INTEGER, PRIMARY KEY(a, b));
""")
# миграция: у старой базы нет колонки games
if "games" not in [c["name"] for c in db.execute("PRAGMA table_info(users)")]:
    db.execute("ALTER TABLE users ADD COLUMN games TEXT DEFAULT ''")
    db.commit()

print("DB path:", os.path.join(DATA_DIR, "allies.db"), "| users in DB:", db.execute("SELECT COUNT(*) FROM users").fetchone()[0], flush=True)

dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎮 Открыть", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await m.answer("Привет! Здесь можно найти союзников для игр 👇", reply_markup=kb)


def auth(request):
    """Проверяет подпись Telegram initData и возвращает данные пользователя."""
    pairs = dict(parse_qsl(request.headers.get("X-Init-Data", ""), keep_blank_values=True))
    got = pairs.pop("hash", None)
    if not got:
        raise web.HTTPUnauthorized()
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        raise web.HTTPUnauthorized()
    if time.time() - int(pairs.get("auth_date", 0)) > 86400:
        raise web.HTTPUnauthorized()
    return json.loads(pairs["user"])


def parse_games(s):
    return [g for g in (s or "").split(",") if g]


def row(r):
    if not r:
        return None
    d = dict(r)
    if "games" in d:
        d["games"] = parse_games(d["games"])
    return d


@web.middleware
async def no_cache(request, handler):
    resp = await handler(request)
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


async def index(request):
    return web.FileResponse(os.path.join(HERE, "index.html"))


async def get_me(request):
    u = auth(request)
    db.execute("UPDATE users SET username=? WHERE id=?", (u.get("username"), u["id"]))
    db.commit()
    r = db.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    print(f"GET /api/me user={u['id']} found={r is not None}", flush=True)
    return web.json_response({"me": row(r)})


async def put_me(request):
    u = auth(request)
    d = await request.json()
    name = str(d.get("name", "")).strip()[:30]
    bio = str(d.get("bio", "")).strip()[:300]
    photo = str(d.get("photo", ""))
    try:
        age = int(d.get("age"))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest()
    if not (name and bio and 12 <= age <= 99):
        raise web.HTTPBadRequest()
    if not photo.startswith("data:image/jpeg;base64,") or len(photo) > 400_000:
        raise web.HTTPBadRequest()
    raw = d.get("games")
    if not isinstance(raw, list):
        raise web.HTTPBadRequest()
    games = []
    for g in raw:
        if isinstance(g, str) and GAME_RE.fullmatch(g) and g not in games:
            games.append(g)
    if not (1 <= len(games) <= MAX_GAMES):
        raise web.HTTPBadRequest()
    db.execute("""INSERT INTO users(id,username,name,age,bio,photo,games) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET username=excluded.username, name=excluded.name,
        age=excluded.age, bio=excluded.bio, photo=excluded.photo, games=excluded.games""",
               (u["id"], u.get("username"), name, age, bio, photo, ",".join(games)))
    db.commit()
    print(f"PUT /api/me user={u['id']} saved", flush=True)
    return web.json_response({"ok": True})


async def del_me(request):
    u = auth(request)
    db.execute("DELETE FROM users WHERE id=?", (u["id"],))
    db.execute("DELETE FROM swipes WHERE a=? OR b=?", (u["id"], u["id"]))
    db.commit()
    return web.json_response({"ok": True})


async def settings(request):
    u = auth(request)
    d = await request.json()
    try:
        a, b = int(d["amin"]), int(d["amax"])
    except (KeyError, TypeError, ValueError):
        raise web.HTTPBadRequest()
    db.execute("UPDATE users SET amin=?, amax=? WHERE id=?",
               (max(12, min(a, b)), min(99, max(a, b)), u["id"]))
    db.commit()
    return web.json_response({"ok": True})


async def nxt(request):
    """Следующие анкеты: сначала те, у кого больше общих игр,
    при равенстве — те, кто уже лайкнул тебя, дальше случайно."""
    u = auth(request)
    me = db.execute("SELECT amin,amax,games FROM users WHERE id=?", (u["id"],)).fetchone()
    if not me:
        raise web.HTTPForbidden()
    mine = set(parse_games(me["games"]))
    cand = db.execute("""
        SELECT id, games,
               EXISTS(SELECT 1 FROM swipes WHERE a=x.id AND b=? AND liked=1) AS likedme
        FROM users x
        WHERE id!=? AND age BETWEEN ? AND ?
          AND NOT EXISTS(SELECT 1 FROM swipes WHERE a=? AND b=x.id)""",
                      (u["id"], u["id"], me["amin"], me["amax"], u["id"])).fetchall()
    scored = sorted(
        ((len(mine & set(parse_games(r["games"]))), r["likedme"], random.random(), r["id"])
         for r in cand), reverse=True)
    ids = [s[3] for s in scored[:5]]
    if not ids:
        return web.json_response({"users": []})
    marks = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in db.execute(
        f"SELECT id,name,age,bio,photo,games FROM users WHERE id IN ({marks})", ids)}
    return web.json_response({"users": [row(rows[i]) for i in ids]})


async def swipe(request):
    u = auth(request)
    d = await request.json()
    target, liked = int(d["target"]), 1 if d.get("like") else 0
    other = db.execute("SELECT name FROM users WHERE id=?", (target,)).fetchone()
    mine = db.execute("SELECT name FROM users WHERE id=?", (u["id"],)).fetchone()
    if not other or not mine or target == u["id"]:
        raise web.HTTPBadRequest()
    db.execute("INSERT OR REPLACE INTO swipes VALUES(?,?,?)", (u["id"], target, liked))
    db.commit()
    match = False
    if liked:
        match = db.execute("SELECT 1 FROM swipes WHERE a=? AND b=? AND liked=1",
                           (target, u["id"])).fetchone() is not None
    if match:
        try:
            await request.app["bot"].send_message(
                target, f"🎉 Новый мэтч с {mine['name']}! Загляни во вкладку «Мэтчи».")
        except Exception:
            pass
    return web.json_response({"match": match, "name": other["name"]})


async def matches(request):
    u = auth(request)
    rows = db.execute("""
        SELECT x.id,x.username,x.name,x.age,x.bio,x.photo,x.games
        FROM swipes s1 JOIN swipes s2 ON s2.a=s1.b AND s2.b=s1.a
        JOIN users x ON x.id=s1.b
        WHERE s1.a=? AND s1.liked=1 AND s2.liked=1 ORDER BY s1.rowid DESC""",
                      (u["id"],)).fetchall()
    return web.json_response({"matches": [row(r) for r in rows]})


async def main():
    bot = Bot(TOKEN)
    app = web.Application(client_max_size=2 * 1024 * 1024, middlewares=[no_cache])
    app["bot"] = bot
    app.add_routes([
        web.get("/", index),
        web.get("/api/me", get_me), web.post("/api/me", put_me), web.delete("/api/me", del_me),
        web.post("/api/settings", settings), web.get("/api/next", nxt),
        web.post("/api/swipe", swipe), web.get("/api/matches", matches),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
        text="Союзники", web_app=WebAppInfo(url=WEBAPP_URL)))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
