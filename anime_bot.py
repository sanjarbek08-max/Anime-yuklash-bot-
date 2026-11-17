"""
Anime bot (tugmali): kod bilan anime ko'rish, admin panel, majburiy obuna,
reyting, baholash, saqlash, statistika, kanalga e'lon.

- Faqat httpx kerak (aiogram yo'q).
- Termux: `python anime_bot.py` (polling).
- Bepul server: WEBHOOK_URL / RENDER_EXTERNAL_URL bo'lsa webhook rejimi + baza zaxirasi.
"""
import asyncio
import hashlib
import html
import json
import logging
import math
import os
import re
import signal
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # loglarda token chiqmasin
log = logging.getLogger("anime")


# ---------- sozlamalar ----------
def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
BOT_TOKEN = os.environ["BOT_TOKEN"]
_adm_list = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
ADMINS = set(_adm_list)
MAIN_ADMIN = _adm_list[0] if _adm_list else 0  # asosiy admin: boshqa admin qo'sha/o'chira oladi
DB_PATH = os.environ.get("DB_PATH", "anime.db")
TZ_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "5"))
TG_HOST = os.environ.get("TG_API_HOST", "https://api.telegram.org")
API = f"{TG_HOST}/bot{BOT_TOKEN}"
http: httpx.AsyncClient
BOT_ID = 0
BOT_USERNAME = ""

WEBHOOK_BASE = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
WEBHOOK_MODE = bool(WEBHOOK_BASE)
PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:48]
BACKUP_ON = os.environ.get("BACKUP", "1" if WEBHOOK_MODE else "0") == "1"
BACKUP_EVERY = int(os.environ.get("BACKUP_MINUTES", "10")) * 60
BACKUP_CHAT = int(os.environ.get("BACKUP_CHAT") or (_adm_list[0] if _adm_list else 0))
BACKUP_NAME = "anime.db"


def restore_db_from_telegram() -> None:
    """Baza fayli yo'q bo'lsa (yangi server), adminning pin qilingan zaxirasidan tiklaydi."""
    if not BACKUP_ON or os.path.exists(DB_PATH) or not BACKUP_CHAT:
        return
    try:
        with httpx.Client(timeout=60) as c:
            chat = c.post(f"{API}/getChat", json={"chat_id": BACKUP_CHAT}).json()
            doc = ((chat.get("result") or {}).get("pinned_message") or {}).get("document")
            if not doc or doc.get("file_name") != BACKUP_NAME:
                logging.warning("Zaxira topilmadi - yangi baza yaratiladi")
                return
            f = c.post(f"{API}/getFile", json={"file_id": doc["file_id"]}).json()
            data = c.get(f"{TG_HOST}/file/bot{BOT_TOKEN}/{f['result']['file_path']}").content
        with open(DB_PATH, "wb") as out:
            out.write(data)
        logging.warning("Baza zaxiradan tiklandi (%d bayt)", len(data))
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"Zaxirani tiklab bo'lmadi, qayta uriniladi: {e!r}")


restore_db_from_telegram()

# ---------- baza ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT,
    joined TEXT, last_seen TEXT, blocked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS anime(
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, descr TEXT, genre TEXT,
    poster TEXT, created TEXT
);
CREATE TABLE IF NOT EXISTS episodes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, anime_id INTEGER, num INTEGER,
    file_id TEXT, ftype TEXT
);
CREATE INDEX IF NOT EXISTS ep_anime ON episodes(anime_id, num);
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE, title TEXT, link TEXT
);
CREATE TABLE IF NOT EXISTS ratings(
    user_id INTEGER, anime_id INTEGER, score INTEGER, PRIMARY KEY(user_id, anime_id)
);
CREATE TABLE IF NOT EXISTS favorites(
    user_id INTEGER, anime_id INTEGER, PRIMARY KEY(user_id, anime_id)
);
CREATE TABLE IF NOT EXISTS views(
    anime_id INTEGER, user_id INTEGER, PRIMARY KEY(anime_id, user_id)
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS news_channels(chat_id INTEGER PRIMARY KEY, title TEXT);
CREATE TABLE IF NOT EXISTS sched(
    id INTEGER PRIMARY KEY AUTOINCREMENT, creator INTEGER, src_chat INTEGER, src_msg INTEGER,
    target TEXT, kind TEXT, hhmm TEXT, date TEXT, weekday INTEGER, next_run TEXT,
    active INTEGER DEFAULT 1, created TEXT, last_run TEXT
);
CREATE TABLE IF NOT EXISTS bjobs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, src_chat INTEGER, src_msg INTEGER,
    anime_id INTEGER, anime_new INTEGER, added INTEGER, target TEXT, status TEXT,
    last_uid INTEGER DEFAULT 0, sent INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
    blocked_n INTEGER DEFAULT 0, ch_done INTEGER DEFAULT 0, ch_ok INTEGER DEFAULT 0,
    ch_fail INTEGER DEFAULT 0, notify INTEGER, created TEXT
);
"""
)
for _col in ("phone TEXT", "joined_at TEXT", "seen_at TEXT", "chat_admin INTEGER", "chat_until TEXT"):
    try:
        db.execute(f"ALTER TABLE users ADD COLUMN {_col}")
    except sqlite3.OperationalError:
        pass  # ustun allaqachon bor
db.commit()

db.commit()


def get_set(key: str, default: str = "") -> str:
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_set(key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
    db.commit()


def now_local() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_HOURS)


def today() -> str:
    return now_local().strftime("%Y-%m-%d")


def now_str() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M")


esc = html.escape


# ---------- Telegram API ----------
class TGError(RuntimeError):
    def __init__(self, desc: str, code: int = 0, retry: int = 0):
        super().__init__(desc)
        self.code, self.retry = code, retry


async def tg(method: str, **params):
    r = await http.post(f"{API}/{method}", json=params, timeout=70)
    data = r.json()
    if not data.get("ok"):
        raise TGError(data.get("description") or f"{method} xato", data.get("error_code", 0),
                      (data.get("parameters") or {}).get("retry_after", 0))
    return data["result"]


async def safe(method: str, **params):
    try:
        return await tg(method, **params)
    except Exception as e:  # noqa: BLE001
        if "not modified" not in str(e):
            log.warning("%s: %s", method, e)
        return None


def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def ubtn(text: str, url: str) -> dict:
    return {"text": text, "url": url}


def kb(rows: list) -> dict:
    return {"inline_keyboard": rows}


async def send(chat_id: int, text: str, markup: dict | None = None) -> dict:
    p = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        p["reply_markup"] = markup
    return await tg("sendMessage", **p)


async def show(chat_id: int, mid: int | None, text: str, markup: dict | None = None) -> None:
    """mid bo'lsa xabarni tahrirlaydi, bo'lmasa yangisini yuboradi."""
    if mid:
        try:
            p = {"chat_id": chat_id, "message_id": mid, "text": text[:4000], "parse_mode": "HTML",
                 "disable_web_page_preview": True}
            if markup:
                p["reply_markup"] = markup
            await tg("editMessageText", **p)
            return
        except Exception as e:  # noqa: BLE001
            if "not modified" in str(e):
                return
            await safe("deleteMessage", chat_id=chat_id, message_id=mid)
    await send(chat_id, text, markup)


# ---------- foydalanuvchilar ----------
def reg_user(u: dict) -> bool:
    """Yangi foydalanuvchi bo'lsa True qaytaradi."""
    uid = u["id"]
    name = u.get("first_name") or ""
    r = db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone()
    is_new = not r
    if r:
        db.execute("UPDATE users SET first_name=?, username=?, last_seen=?, seen_at=?, blocked=0 WHERE user_id=?",
                   (name, u.get("username") or "", today(), now_str(), uid))
    else:
        db.execute("INSERT INTO users(user_id,first_name,username,joined,last_seen,joined_at,seen_at) VALUES(?,?,?,?,?,?,?)",
                   (uid, name, u.get("username") or "", today(), today(), now_str(), now_str()))
    db.commit()
    return is_new


# ---------- majburiy obuna ----------
sub_cache: dict[int, float] = {}


def channels() -> list:
    return db.execute("SELECT * FROM channels ORDER BY id").fetchall()


async def missing_channels(uid: int) -> list:
    if uid in ADMINS or time.time() - sub_cache.get(uid, 0) < 300:
        return []
    miss = []
    for ch in channels():
        try:
            m = await tg("getChatMember", chat_id=ch["chat_id"], user_id=uid)
            st = m.get("status")
            if st in ("left", "kicked") or (st == "restricted" and not m.get("is_member")):
                miss.append(ch)
        except Exception as e:  # noqa: BLE001
            # bot kanalda admin bo'lmasa tekshirib bo'lmaydi: foydalanuvchini to'smaymiz
            log.warning("obuna tekshiruvi (%s): %s", ch["chat_id"], e)
    if not miss:
        sub_cache[uid] = time.time()
    return miss


pending: dict[int, str] = {}  # telefon kutilayotganda deep-link payload


def has_phone(uid: int) -> bool:
    r = db.execute("SELECT phone FROM users WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r["phone"])


def user_line(r) -> str:
    un = f"@{esc(r['username'])}" if r["username"] else "—"
    return f"👤 <b>{esc(r['first_name'] or 'Noma' + chr(39) + 'lum')}</b>\n🔗 {un}\n🆔 <code>{r['user_id']}</code>"


async def notify_main(uid: int, title: str) -> None:
    """Asosiy adminga foydalanuvchi haqida xabar."""
    r = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not r or not MAIN_ADMIN or uid in ADMINS:
        return
    ph = f"\n📱 <code>{esc(r['phone'])}</code>" if r["phone"] else ""
    await safe("sendMessage", chat_id=MAIN_ADMIN, parse_mode="HTML",
               text=f"{title}\n\n{user_line(r)}{ph}\n🕐 {now_str()}",
               reply_markup=kb([[btn("✉️ Xabar yozish", f"ad:wr:{uid}")]]))


async def gate(uid: int, payload: str = "-") -> bool:
    """Obuna bo'lmagan bo'lsa kanallar ro'yxatini yuboradi va True qaytaradi."""
    miss = await missing_channels(uid)
    if not miss:
        if uid in ADMINS or has_phone(uid):
            return False
        if payload != "-":
            pending[uid] = payload
        await tg("sendMessage", chat_id=uid, parse_mode="HTML",
                 text="📱 <b>Ro'yxatdan o'tish</b>\n\nDavom etish uchun pastdagi <b>«📱 Raqamni ulashish»</b> tugmasini bosing. "
                      "Raqamingiz Telegramdan o'zi yuboriladi, yozish shart emas.",
                 reply_markup={"keyboard": [[{"text": "📱 Raqamni ulashish", "request_contact": True}]],
                               "resize_keyboard": True, "one_time_keyboard": True})
        return True
    rows = [[ubtn(f"📢 {c['title']}", c["link"])] for c in miss]
    rows.append([btn("✅ Tekshirish", f"chk:{payload}")])
    await send(uid, "Botdan foydalanish uchun avval quyidagi kanallarga obuna bo'ling, so'ng "
                    "<b>✅ Tekshirish</b> tugmasini bosing:", kb(rows))
    return True


# ---------- admin bilan suhbat ----------
def chat_deadline() -> str:
    return (now_local() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")


def chat_open(uid: int) -> bool:
    r = db.execute("SELECT chat_until FROM users WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r["chat_until"] and r["chat_until"] >= now_str())


async def to_admin(uid: int, msg: dict) -> None:
    """Foydalanuvchining xabarini suhbatni boshlagan adminga yetkazadi."""
    r = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    admin = (r["chat_admin"] if r else 0) or MAIN_ADMIN
    try:
        await tg("sendMessage", chat_id=admin, parse_mode="HTML", text=f"💬 <b>Foydalanuvchidan xabar</b>\n{user_line(r)}")
        await tg("copyMessage", chat_id=admin, from_chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                 reply_markup=kb([[btn("✉️ Javob berish", f"ad:wr:{uid}")]]))
        db.execute("UPDATE users SET chat_until=? WHERE user_id=?", (chat_deadline(), uid))
        db.commit()
        await send(uid, "✅ Xabaringiz adminga yuborildi.", kb([[btn("❌ Suhbatni tugatish", "uc:x")]]))
    except Exception as e:  # noqa: BLE001
        log.warning("adminga yetkazilmadi: %r", e)
        await send(uid, "❌ Xabarni yuborib bo'lmadi, keyinroq urinib ko'ring.")


# ---------- anime kartasi ----------
def rating_of(aid: int) -> tuple[float, int]:
    r = db.execute("SELECT AVG(score) a, COUNT(*) n FROM ratings WHERE anime_id=?", (aid,)).fetchone()
    return (r["a"] or 0.0, r["n"])


def card_text(a: sqlite3.Row) -> str:
    avg, n = rating_of(a["id"])
    views = db.execute("SELECT COUNT(*) FROM views WHERE anime_id=?", (a["id"],)).fetchone()[0]
    eps = db.execute("SELECT COUNT(*) FROM episodes WHERE anime_id=?", (a["id"],)).fetchone()[0]
    lines = [f"🎬 <b>{esc(a['title'])}</b>", f"🔢 Kod: <code>{a['id']}</code>"]
    if a["genre"]:
        lines.append(f"🎭 Janr: {esc(a['genre'])}")
    lines.append(f"⭐ {avg:.1f} ({n} ovoz)" if n else "⭐ Hali baholanmagan")
    lines.append(f"👁 {views} · 📺 {eps} qism")
    if a["descr"]:
        lines += ["", esc(a["descr"][:600])]
    return "\n".join(lines)


EP_PER_PAGE = 20


def card_kb(a: sqlite3.Row, uid: int, page: int = 0, rating: bool = False) -> dict:
    aid = a["id"]
    nums = [r["num"] for r in db.execute("SELECT num FROM episodes WHERE anime_id=? ORDER BY num", (aid,))]
    pages = max(1, math.ceil(len(nums) / EP_PER_PAGE))
    page = min(max(page, 0), pages - 1)
    chunk = nums[page * EP_PER_PAGE : (page + 1) * EP_PER_PAGE]
    rows = [[btn(f"{n}", f"e:{aid}:{n}") for n in chunk[i : i + 5]] for i in range(0, len(chunk), 5)]
    if pages > 1:
        rows.append([btn("⬅️", f"pg:{aid}:{page - 1}") if page > 0 else btn("·", "noop"),
                     btn(f"{page + 1}/{pages}", "noop"),
                     btn("➡️", f"pg:{aid}:{page + 1}") if page < pages - 1 else btn("·", "noop")])
    if rating:
        rows.append([btn(f"{s}⭐", f"rs:{aid}:{s}") for s in range(1, 6)])
        rows.append([btn("🔙 Orqaga", f"pg:{aid}:{page}")])
    else:
        fav = db.execute("SELECT 1 FROM favorites WHERE user_id=? AND anime_id=?", (uid, aid)).fetchone()
        rows.append([btn("⭐ Baholash", f"r:{aid}"),
                     btn("✅ Saqlangan" if fav else "💾 Saqlash", f"f:{aid}")])
        rows.append([btn("🏠 Menyu", "menu")])
    return kb(rows)


async def send_card(uid: int, aid: int) -> None:
    a = db.execute("SELECT * FROM anime WHERE id=?", (aid,)).fetchone()
    if not a:
        await send(uid, "❌ Bunday kodli anime topilmadi.", kb([[btn("🏠 Menyu", "menu")]]))
        return
    db.execute("INSERT OR IGNORE INTO views(anime_id,user_id) VALUES(?,?)", (aid, uid))
    db.commit()
    text, markup = card_text(a), card_kb(a, uid)
    if a["poster"]:
        try:
            await tg("sendPhoto", chat_id=uid, photo=a["poster"], caption=text, parse_mode="HTML",
                     reply_markup=markup)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("poster yuborilmadi: %s", e)
    await send(uid, text, markup)


async def refresh_card(msg: dict, aid: int, uid: int, page: int = 0, rating: bool = False) -> None:
    a = db.execute("SELECT * FROM anime WHERE id=?", (aid,)).fetchone()
    if not a:
        return
    p = {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"], "parse_mode": "HTML",
         "reply_markup": card_kb(a, uid, page, rating)}
    if msg.get("photo"):
        await safe("editMessageCaption", caption=card_text(a), **p)
    else:
        await safe("editMessageText", text=card_text(a), **p)


async def send_episode(uid: int, aid: int, num: int) -> None:
    a = db.execute("SELECT * FROM anime WHERE id=?", (aid,)).fetchone()
    e = db.execute("SELECT * FROM episodes WHERE anime_id=? AND num=?", (aid, num)).fetchone()
    if not a or not e:
        await send(uid, "❌ Qism topilmadi.")
        return
    nxt = db.execute("SELECT 1 FROM episodes WHERE anime_id=? AND num=?", (aid, num + 1)).fetchone()
    rows = []
    if nxt:
        rows.append([btn("➡️ Keyingi qism", f"e:{aid}:{num + 1}")])
    rows.append([btn("📋 Barcha qismlar", f"a:{aid}"), btn("🏠 Menyu", "menu")])
    cap = f"🎬 <b>{esc(a['title'])}</b> — {num}-qism"
    method, field = ("sendVideo", "video") if e["ftype"] == "video" else ("sendDocument", "document")
    await tg(method, chat_id=uid, caption=cap, parse_mode="HTML", reply_markup=kb(rows), **{field: e["file_id"]})


# ---------- ro'yxatlar ----------
LIST_TITLES = {"top": "🏆 Eng yuqori reytingli animelar", "pop": "🔥 Eng ommabop animelar",
               "all": "📚 Barcha animelar", "fav": "⭐ Saqlanganlar"}


def list_items(kind: str, uid: int) -> list[tuple[int, str]]:
    if kind == "top":
        rows = db.execute(
            "SELECT a.id, a.title, AVG(r.score) s, COUNT(*) n FROM anime a JOIN ratings r ON r.anime_id=a.id "
            "GROUP BY a.id ORDER BY s DESC, n DESC LIMIT 20").fetchall()
        return [(r["id"], f"{r['title']} · ⭐{r['s']:.1f} ({r['n']})") for r in rows]
    if kind == "pop":
        rows = db.execute(
            "SELECT a.id, a.title, (SELECT COUNT(*) FROM views v WHERE v.anime_id=a.id) c FROM anime a "
            "ORDER BY c DESC, a.id DESC LIMIT 20").fetchall()
        return [(r["id"], f"{r['title']} · 👁{r['c']}") for r in rows]
    if kind == "fav":
        rows = db.execute("SELECT a.id, a.title FROM anime a JOIN favorites f ON f.anime_id=a.id "
                          "WHERE f.user_id=? ORDER BY a.id DESC", (uid,)).fetchall()
        return [(r["id"], r["title"]) for r in rows]
    rows = db.execute("SELECT id, title FROM anime ORDER BY id DESC").fetchall()
    return [(r["id"], r["title"]) for r in rows]


def paged_kb(items: list[tuple[int, str]], page: int, item_cb, nav_cb, per: int = 8, back: str = "menu") -> dict:
    pages = max(1, math.ceil(len(items) / per))
    page = min(max(page, 0), pages - 1)
    rows = [[btn(t[:60], item_cb(i))] for i, t in items[page * per : (page + 1) * per]]
    if pages > 1:
        rows.append([btn("⬅️", nav_cb(page - 1)) if page > 0 else btn("·", "noop"),
                     btn(f"{page + 1}/{pages}", "noop"),
                     btn("➡️", nav_cb(page + 1)) if page < pages - 1 else btn("·", "noop")])
    rows.append([btn("🔙 Orqaga" if back != "menu" else "🏠 Menyu", back)])
    return kb(rows)


async def show_list(uid: int, mid: int | None, kind: str, page: int) -> None:
    items = list_items(kind, uid)
    if not items:
        await show(uid, mid, f"{LIST_TITLES[kind]}\n\nHozircha bo'sh.", kb([[btn("🏠 Menyu", "menu")]]))
        return
    await show(uid, mid, f"<b>{LIST_TITLES[kind]}</b>",
               paged_kb(items, page, lambda i: f"a:{i}", lambda p: f"l:{kind}:{p}"))


def rkb(rows: list) -> dict:
    """Ekranning pastidagi doimiy klaviatura."""
    return {"keyboard": [[{"text": t} for t in r] for r in rows], "resize_keyboard": True,
            "is_persistent": True, "input_field_placeholder": "Anime kodi yoki nomini yozing..."}


USER_BTN = {"top": "🏆 Reyting", "pop": "🔥 Ommabop", "all": "📚 Barcha animelar", "fav": "⭐ Saqlanganlar"}
USER_BTN_REV = {v: k for k, v in USER_BTN.items()}
B_ADMIN, B_MENU = "👑 Admin panel", "🏠 Menyu"
ADMIN_BTN = {"ad:add": "➕ Anime qo'shish", "ad:pick:addep:0": "🎞 Qism qo'shish",
             "ad:pick:del:0": "🗑 Anime o'chirish", "ad:pick:delep:0": "✂️ Qism o'chirish",
             "ad:ch": "📢 Majburiy kanallar", "ad:news": "📣 E'lon kanallari",
             "ad:bc": "📨 Xabar yuborish", "ad:sl": "⏰ Rejalangan xabarlar", "ad:stat": "📊 Statistika",
             "ad:am": "👥 Adminlar", "ad:us": "👤 Foydalanuvchilar"}
ADMIN_BTN_REV = {v: k for k, v in ADMIN_BTN.items()}
ADMIN_BTN_REV["📣 E'lon kanali"] = "ad:news"  # eski yozuv (eski klaviatura uchun)
ALL_BTN_TEXTS = set(USER_BTN.values()) | set(ADMIN_BTN_REV) | {B_ADMIN, B_MENU}


def main_menu(uid: int) -> dict:
    rows = [[USER_BTN["top"], USER_BTN["pop"]], [USER_BTN["all"], USER_BTN["fav"]]]
    if uid in ADMINS:
        rows.append([B_ADMIN])
    return rkb(rows)


MENU_TEXT = ("🎌 <b>Anime bot</b>\n\nAnime <b>kodini</b> yuboring (masalan: <code>12</code>) yoki "
             "<b>nomini</b> yozib qidiring. Menyu pastdagi tugmalarda.")


async def show_menu(uid: int, mid: int | None = None) -> None:
    if mid:
        await safe("deleteMessage", chat_id=uid, message_id=mid)
    await send(uid, MENU_TEXT, main_menu(uid))


# ---------- admin: yordamchilar ----------
state: dict[int, dict] = {}  # admin bosqichlari


def admin_kb(uid: int = 0) -> dict:
    t = ADMIN_BTN
    rows = [[t["ad:add"], t["ad:pick:addep:0"]], [t["ad:pick:del:0"], t["ad:pick:delep:0"]],
            [t["ad:ch"], t["ad:news"]], [t["ad:bc"], t["ad:sl"]], [t["ad:us"], t["ad:stat"]]]
    # "Adminlar" tugmasi faqat asosiy adminda bor
    rows.append([t["ad:am"], B_MENU] if uid == MAIN_ADMIN else [B_MENU])
    return rkb(rows)


async def show_admin(uid: int, mid: int | None = None) -> None:
    if mid:
        await safe("deleteMessage", chat_id=uid, message_id=mid)
    await send(uid, "👑 <b>Admin panel</b>\nTugmalar pastda.", admin_kb(uid))


def chat_ref(msg: dict):
    fo = msg.get("forward_origin") or {}
    if fo.get("type") == "channel":
        return fo["chat"]["id"]
    if msg.get("forward_from_chat"):
        return msg["forward_from_chat"]["id"]
    t = (msg.get("text") or "").strip()
    if re.fullmatch(r"-?\d{5,}", t):
        return int(t)
    m = re.search(r"(?:t\.me/|@)([A-Za-z0-9_]{4,})", t)
    if m:
        return "@" + m.group(1)
    return None


async def verify_channel(ref, need_post: bool = False) -> dict:
    try:
        ch = await tg("getChat", chat_id=ref)
    except Exception:  # noqa: BLE001
        raise ValueError("❌ Kanal topilmadi. @username, havola yoki ID ni tekshiring.")
    if ch.get("type") not in ("channel", "supergroup", "group"):
        raise ValueError("❌ Bu kanal yoki guruh emas.")
    try:
        m = await tg("getChatMember", chat_id=ch["id"], user_id=BOT_ID)
    except Exception:  # noqa: BLE001
        raise ValueError("❌ Bot bu kanalda admin emas. Avval botni kanalga admin qiling.")
    if m.get("status") not in ("administrator", "creator"):
        raise ValueError("❌ Botni kanalga admin qiling, keyin qayta urinib ko'ring.")
    if need_post and ch.get("type") == "channel" and m.get("status") == "administrator" \
            and not m.get("can_post_messages", True):
        raise ValueError("❌ Botga kanalda «Xabar yuborish» huquqini bering.")
    link = f"https://t.me/{ch['username']}" if ch.get("username") else None
    if not link and not need_post:
        try:
            link = await tg("exportChatInviteLink", chat_id=ch["id"])
        except Exception:  # noqa: BLE001
            raise ValueError("❌ Yopiq kanal uchun botga «Foydalanuvchilarni taklif qilish» huquqini bering.")
    return {"chat_id": ch["id"], "title": ch.get("title") or str(ch["id"]), "link": link or ""}


# ---------- e'lon va xabar yuborish ----------
TARGET_NAMES = {"users": "👥 Foydalanuvchilar", "channels": "📣 Kanallar", "both": "👥📣 Ikkalasi"}
WEEKDAYS = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
job_lock = asyncio.Lock()


def news_channels() -> list:
    return db.execute("SELECT * FROM news_channels ORDER BY rowid").fetchall()


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    tasks.add(t)
    t.add_done_callback(tasks.discard)


async def announce_to(chat_id: int, aid: int, new: bool, added: int) -> None:
    """Anime e'lonini bitta chatga (foydalanuvchi yoki kanal) yuboradi."""
    a = db.execute("SELECT * FROM anime WHERE id=?", (aid,)).fetchone()
    if not a:
        return
    total = db.execute("SELECT COUNT(*) FROM episodes WHERE anime_id=?", (aid,)).fetchone()[0]
    head = "🆕 <b>Yangi anime!</b>" if new else f"🆕 <b>Yangi qism{'lar' if added > 1 else ''} qo'shildi!</b>"
    lines = [head, "", f"🎬 <b>{esc(a['title'])}</b>", f"🔢 Kod: <code>{aid}</code>"]
    if a["genre"]:
        lines.append(f"🎭 Janr: {esc(a['genre'])}")
    lines.append(f"📺 Jami qismlar: {total}")
    if a["descr"] and new:
        lines += ["", esc(a["descr"][:500])]
    markup = kb([[ubtn("▶️ Ko'rish", f"https://t.me/{BOT_USERNAME}?start=a{aid}")]])
    text = "\n".join(lines)
    if a["poster"]:
        await tg("sendPhoto", chat_id=chat_id, photo=a["poster"], caption=text[:1020], parse_mode="HTML",
                 reply_markup=markup)
    else:
        await send(chat_id, text, markup)


def start_job(kind: str, target: str, notify: int, src_chat: int = 0, src_msg: int = 0,
              anime_id: int = 0, anime_new: int = 0, added: int = 0) -> int:
    cur = db.execute(
        "INSERT INTO bjobs(kind,src_chat,src_msg,anime_id,anime_new,added,target,status,notify,created) "
        "VALUES(?,?,?,?,?,?,?,'running',?,?)",
        (kind, src_chat, src_msg, anime_id, anime_new, added, target, notify, today()))
    db.commit()
    spawn(run_job(cur.lastrowid))
    return cur.lastrowid


def resume_jobs() -> None:
    """Server qayta ishga tushganda yarim qolgan yuborishlarni davom ettiradi."""
    for r in db.execute("SELECT id FROM bjobs WHERE status='running'").fetchall():
        spawn(run_job(r["id"]))


async def deliver(job: sqlite3.Row, chat_id: int) -> str:
    """'ok' | 'blocked' | 'fail'"""
    for _ in range(4):
        try:
            if job["kind"] == "anime":
                await announce_to(chat_id, job["anime_id"], bool(job["anime_new"]), job["added"] or 1)
            else:
                await tg("copyMessage", chat_id=chat_id, from_chat_id=job["src_chat"], message_id=job["src_msg"])
            return "ok"
        except TGError as e:
            if e.code == 429:
                await asyncio.sleep(min(e.retry or 1, 60) + 1)
                continue
            if e.code == 403 or any(x in str(e).lower() for x in ("blocked", "deactivated", "chat not found")):
                return "blocked"
            log.warning("yuborilmadi (%s): %s", chat_id, e)
            return "fail"
        except Exception as e:  # noqa: BLE001
            log.warning("yuborishda xato (%s): %r", chat_id, e)
            await asyncio.sleep(1)
    return "fail"


async def run_job(jid: int) -> None:
    async with job_lock:
        job = db.execute("SELECT * FROM bjobs WHERE id=?", (jid,)).fetchone()
        if not job or job["status"] != "running":
            return
        sent, failed, blocked, last = job["sent"], job["failed"], job["blocked_n"], job["last_uid"]
        ch_ok, ch_fail = job["ch_ok"], job["ch_fail"]
        if job["target"] in ("channels", "both") and not job["ch_done"]:
            for ch in news_channels():
                if await deliver(job, ch["chat_id"]) == "ok":
                    ch_ok += 1
                else:
                    ch_fail += 1
            db.execute("UPDATE bjobs SET ch_done=1, ch_ok=?, ch_fail=? WHERE id=?", (ch_ok, ch_fail, jid))
            db.commit()
        if job["target"] in ("users", "both"):
            while True:
                rows = db.execute("SELECT user_id FROM users WHERE blocked=0 AND user_id>? ORDER BY user_id LIMIT 25",
                                  (last,)).fetchall()
                if not rows:
                    break
                for r in rows:
                    res = await deliver(job, r["user_id"])
                    if res == "ok":
                        sent += 1
                    elif res == "blocked":
                        blocked += 1
                        db.execute("UPDATE users SET blocked=1 WHERE user_id=?", (r["user_id"],))
                    else:
                        failed += 1
                    last = r["user_id"]
                    await asyncio.sleep(0.05)  # soniyasiga ~20 ta (Telegram limitidan past)
                db.execute("UPDATE bjobs SET last_uid=?, sent=?, failed=?, blocked_n=? WHERE id=?",
                           (last, sent, failed, blocked, jid))
                db.commit()
        db.execute("UPDATE bjobs SET status='done', last_uid=?, sent=?, failed=?, blocked_n=? WHERE id=?",
                   (last, sent, failed, blocked, jid))
        db.commit()
    lines = ["✅ <b>Yuborish tugadi</b>" if job["kind"] == "copy" else "✅ <b>Anime e'loni yuborildi</b>"]
    if job["target"] in ("users", "both"):
        lines.append(f"👥 Yetib bordi: <b>{sent}</b> · botni bloklagan: {blocked} · xato: {failed}")
    if job["target"] in ("channels", "both"):
        lines.append(f"📣 Kanallar: <b>{ch_ok}</b> ta yuborildi, {ch_fail} ta xato" if ch_ok + ch_fail
                     else "📣 E'lon kanallari qo'shilmagan")
    if job["notify"]:
        await safe("sendMessage", chat_id=job["notify"], text="\n".join(lines), parse_mode="HTML")


# ---------- taymer ----------
def naive_now() -> datetime:
    return now_local().replace(tzinfo=None, second=0, microsecond=0)


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def parse_dt(text: str) -> datetime:
    for f in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            dt = datetime.strptime(text.strip(), f)
            return dt
        except ValueError:
            pass
    raise ValueError("Format noto'g'ri. Masalan: 2026-11-20 18:30 yoki 20.11.2026 18:30")


def parse_hm(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", text.strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ValueError("Vaqtni shunday yozing: 09:00")
    return int(m[1]), int(m[2])


def first_run(kind: str, hh: int, mm: int, weekday: int | None = None) -> datetime:
    now = naive_now()
    dt = now.replace(hour=hh, minute=mm)
    if kind == "daily":
        return dt if dt > now else dt + timedelta(days=1)
    dt += timedelta(days=(weekday - now.weekday()) % 7)
    return dt if dt > now else dt + timedelta(days=7)


def add_sched(uid: int, st: dict, dt: datetime) -> int:
    cur = db.execute(
        "INSERT INTO sched(creator,src_chat,src_msg,target,kind,hhmm,date,weekday,next_run,created) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uid, st["src"][0], st["src"][1], st["target"], st["kind"], dt.strftime("%H:%M"),
         dt.strftime("%Y-%m-%d") if st["kind"] == "once" else None, st.get("weekday"), fmt_dt(dt), today()))
    db.commit()
    return cur.lastrowid


def sched_desc(s: sqlite3.Row) -> str:
    if s["kind"] == "once":
        return f"📅 {s['date']} {s['hhmm']}"
    if s["kind"] == "daily":
        return f"🔁 Har kuni {s['hhmm']}"
    return f"🗓 Har {WEEKDAYS[s['weekday']].lower()} {s['hhmm']}"


async def sched_tick() -> None:
    now = naive_now()
    for s in db.execute("SELECT * FROM sched WHERE active=1 AND next_run<=?", (fmt_dt(now),)).fetchall():
        due = datetime.strptime(s["next_run"], "%Y-%m-%d %H:%M")
        if (now - due).total_seconds() <= 3 * 3600:
            start_job("copy", s["target"], s["creator"], s["src_chat"], s["src_msg"])
            log.info("Rejali xabar #%s yuborilmoqda", s["id"])
        else:
            await safe("sendMessage", chat_id=s["creator"],
                       text=f"⚠️ Rejali xabar #{s['id']} ({s['next_run']}) bot o'chiq bo'lgani uchun o'tkazib yuborildi.")
        if s["kind"] == "once":
            db.execute("UPDATE sched SET active=0, last_run=? WHERE id=?", (fmt_dt(now), s["id"]))
        else:
            step = timedelta(days=1 if s["kind"] == "daily" else 7)
            nxt = due + step
            while nxt <= now:
                nxt += step
            db.execute("UPDATE sched SET next_run=?, last_run=? WHERE id=?", (fmt_dt(nxt), fmt_dt(now), s["id"]))
        db.commit()


async def scheduler() -> None:
    while True:
        try:
            await sched_tick()
        except Exception as e:  # noqa: BLE001
            log.error("scheduler: %r", e)
        await asyncio.sleep(30)


def target_kb() -> dict:
    return kb([[btn("👥 Foydalanuvchilarga", "ad:bt:users")], [btn("📣 Kanallarga", "ad:bt:channels")],
               [btn("👥📣 Ikkalasiga", "ad:bt:both")], [btn("❌ Bekor qilish", "ad")]])


def delete_anime(aid: int) -> None:
    for t in ("episodes", "ratings", "favorites", "views"):
        db.execute(f"DELETE FROM {t} WHERE anime_id=?", (aid,))
    db.execute("DELETE FROM anime WHERE id=?", (aid,))
    db.commit()


# ---------- foydalanuvchilar ma'lumoti ----------
U_FILTERS = {
    "all": ("📋 Hammasi", "1=1", "u.rowid DESC"),
    "act": ("🟢 Faol (bloklamagan)", "u.blocked=0", "u.last_seen DESC, u.rowid DESC"),
    "blk": ("🚫 Bloklaganlar", "u.blocked=1", "u.rowid DESC"),
    "rec": ("🔥 Oxirgi 7 kunda faol", "u.blocked=0 AND u.last_seen>=?", "u.last_seen DESC, u.rowid DESC"),
    "top": ("🏆 Eng ko'p anime ko'rganlar", "vc>0", "vc DESC, u.rowid DESC"),
    "ph": ("📱 Raqami borlar", "u.phone IS NOT NULL AND u.phone<>''", "u.rowid DESC"),
}


def _users_query(flt: str) -> list:
    _, cond, order = U_FILTERS[flt]
    args = ((now_local() - timedelta(days=7)).strftime("%Y-%m-%d"),) if flt == "rec" else ()
    sql = ("SELECT * FROM (SELECT u.rowid AS rid, u.*, (SELECT COUNT(*) FROM views v WHERE v.user_id=u.user_id) vc FROM users u) u "
           f"WHERE {cond.replace('vc>0', 'u.vc>0')} ORDER BY {order.replace('vc DESC', 'u.vc DESC').replace('u.rowid', 'u.rid')}")
    return db.execute(sql, args).fetchall()


def users_summary() -> str:
    q = lambda s, *a: db.execute(s, a).fetchone()[0]  # noqa: E731
    week = (now_local() - timedelta(days=7)).strftime("%Y-%m-%d")
    ph_n = q("SELECT COUNT(*) FROM users WHERE phone IS NOT NULL AND phone<>''")
    return "\n".join([
        "👤 <b>Foydalanuvchilar</b>", "",
        f"👥 Jami: <b>{q('SELECT COUNT(*) FROM users')}</b>",
        f"🟢 Faol (bloklamagan): <b>{q('SELECT COUNT(*) FROM users WHERE blocked=0')}</b>",
        f"🚫 Botni bloklagan: <b>{q('SELECT COUNT(*) FROM users WHERE blocked=1')}</b>",
        f"🔥 Oxirgi 7 kunda faol: <b>{q('SELECT COUNT(*) FROM users WHERE blocked=0 AND last_seen>=?', week)}</b>",
        f"🎬 Anime ko'rganlar: <b>{q('SELECT COUNT(DISTINCT user_id) FROM views')}</b>",
        f"📱 Raqami borlar: <b>{ph_n}</b>",
        "", "Ro'yxatni tanlang:"])


def users_filter_kb() -> dict:
    rows = [[btn(v[0], f"ad:us:{k}:0")] for k, v in U_FILTERS.items()]
    rows.append([btn("🔙 Orqaga", "ad")])
    return kb(rows)


def u_label(r) -> str:
    st = "🚫" if r["blocked"] else "🟢"
    name = (r["first_name"] or "Noma'lum")[:18]
    un = f" @{r['username']}" if r["username"] else ""
    return f"{st} {name}{un} · 🎬{r['vc']}"


async def show_users(uid: int, mid: int | None, flt: str, page: int) -> None:
    if flt not in U_FILTERS:
        flt = "all"
    rows_db = _users_query(flt)
    title = U_FILTERS[flt][0]
    if not rows_db:
        await show(uid, mid, f"<b>{title}</b>\n\nBu bo'limda hozircha hech kim yo'q.",
                   kb([[btn("🔙 Orqaga", "ad:us")]]))
        return
    per = 8
    pages = max(1, math.ceil(len(rows_db) / per))
    page = min(max(page, 0), pages - 1)
    kbrows = [[btn(u_label(r), f"ad:uv:{r['user_id']}:{flt}:{page}")] for r in rows_db[page * per:(page + 1) * per]]
    if pages > 1:
        kbrows.append([btn("⬅️", f"ad:us:{flt}:{page - 1}") if page > 0 else btn("·", "noop"),
                       btn(f"{page + 1}/{pages}", "noop"),
                       btn("➡️", f"ad:us:{flt}:{page + 1}") if page < pages - 1 else btn("·", "noop")])
    kbrows.append([btn("🔙 Orqaga", "ad:us")])
    await show(uid, mid, f"<b>{title}</b> — {len(rows_db)} ta\n🟢 faol · 🚫 bloklagan · 🎬 ko'rgan anime soni\n"
                         "Batafsil ko'rish uchun ustiga bosing.", kb(kbrows))


async def show_user_card(uid: int, mid: int | None, tid: int, flt: str, page: int) -> None:
    r = db.execute("SELECT * FROM users WHERE user_id=?", (tid,)).fetchone()
    back = kb([[btn("🔙 Ro'yxatga", f"ad:us:{flt}:{page}")]])
    if not r:
        await show(uid, mid, "Foydalanuvchi topilmadi.", back)
        return
    titles = db.execute("SELECT a.id, a.title FROM views v JOIN anime a ON a.id=v.anime_id WHERE v.user_id=? "
                        "ORDER BY a.title", (tid,)).fetchall()
    ph = r["phone"] or ""
    nm = esc(r["first_name"] or "Noma'lum")
    ph_txt = f"<code>{esc(ph)}</code>" if ph else "yo'q (foydalanuvchi bermagan)"
    lines = [
        f"👤 <b>{nm}</b>",
        f"🔗 Username: {('@' + esc(r['username'])) if r['username'] else '—'}",
        f"🆔 ID: <code>{tid}</code>",
        f"📱 Telefon: {ph_txt}",
        f"📅 Ro'yxatdan o'tgan (start): <b>{r['joined_at'] or r['joined'] or '—'}</b>",
        f"🕐 Oxirgi faolligi: <b>{r['seen_at'] or r['last_seen'] or '—'}</b>",
        "📌 Holati: " + ("🚫 <b>botni bloklagan</b>" if r["blocked"] else "🟢 <b>faol</b>"),
        "", f"🎬 Ko'rgan animelari: <b>{len(titles)}</b> ta",
    ]
    for a in titles[:40]:
        lines.append(f"  • {esc(a['title'])} (kod {a['id']})")
    if len(titles) > 40:
        lines.append(f"  … va yana {len(titles) - 40} ta")
    fav = db.execute("SELECT COUNT(*) FROM favorites WHERE user_id=?", (tid,)).fetchone()[0]
    rat = db.execute("SELECT COUNT(*) FROM ratings WHERE user_id=?", (tid,)).fetchone()[0]
    lines.append(f"\n⭐ Saqlagan: {fav} · baholagan: {rat}")
    rows = []
    if not r["blocked"]:
        rows.append([btn("✉️ Xabar yozish", f"ad:wr:{tid}")])
    if r["username"]:
        rows.append([ubtn("💬 Profilini ochish", f"https://t.me/{r['username']}")])
    rows.append([btn("🔙 Ro'yxatga", f"ad:us:{flt}:{page}")])
    await show(uid, mid, "\n".join(lines), kb(rows))


def stats_text() -> str:
    q = lambda s, *a: db.execute(s, a).fetchone()[0]  # noqa: E731
    t = today()
    lines = [
        "📊 <b>Statistika</b>", "",
        f"👥 Foydalanuvchilar: <b>{q('SELECT COUNT(*) FROM users')}</b>",
        f"🆕 Bugun qo'shilgan: <b>{q('SELECT COUNT(*) FROM users WHERE joined=?', t)}</b>",
        f"🟢 Bugun faol: <b>{q('SELECT COUNT(*) FROM users WHERE last_seen=?', t)}</b>",
        f"🚫 Botni bloklagan: <b>{q('SELECT COUNT(*) FROM users WHERE blocked=1')}</b>", "",
        f"🎬 Animelar: <b>{q('SELECT COUNT(*) FROM anime')}</b>",
        f"📺 Qismlar: <b>{q('SELECT COUNT(*) FROM episodes')}</b>",
        f"⭐ Baholar: <b>{q('SELECT COUNT(*) FROM ratings')}</b>",
        f"💾 Saqlashlar: <b>{q('SELECT COUNT(*) FROM favorites')}</b>",
        f"📢 Majburiy kanallar: <b>{q('SELECT COUNT(*) FROM channels')}</b>",
        f"📣 E'lon kanallari: <b>{q('SELECT COUNT(*) FROM news_channels')}</b>",
        f"⏰ Rejalangan xabarlar: <b>{q('SELECT COUNT(*) FROM sched WHERE active=1')}</b>",
    ]
    pop = list_items("pop", 0)[:5]
    if pop:
        lines += ["", "<b>🔥 Top ko'rilganlar:</b>"] + [f"{i}. {esc(t_)}" for i, (_, t_) in enumerate(pop, 1)]
    top = list_items("top", 0)[:5]
    if top:
        lines += ["", "<b>🏆 Top reyting:</b>"] + [f"{i}. {esc(t_)}" for i, (_, t_) in enumerate(top, 1)]
    return "\n".join(lines)


# ---------- admin: tugmalar ----------
async def on_admin_cb(uid: int, msg: dict, data: str) -> None:
    chat_id, mid = uid, msg.get("message_id")
    p = data.split(":")
    sub = p[1] if len(p) > 1 else ""
    if sub not in ("done", "bt", "bw", "bd", "bgo"):
        state.pop(uid, None)

    if sub == "" :
        await show_admin(uid, mid)

    elif sub in ("am", "amadd", "amdel", "amok") and uid != MAIN_ADMIN:
        await send(uid, "⛔ Adminlarni faqat asosiy admin boshqara oladi.")

    elif sub == "am":
        rows_db = db.execute("SELECT * FROM admins ORDER BY rowid").fetchall()
        lines = ["👥 <b>Adminlar</b>", "", f"👑 Asosiy admin: <code>{MAIN_ADMIN}</code>"]
        extra_env = [a for a in _adm_list[1:]]
        for a in extra_env:
            lines.append(f"🔒 <code>{a}</code> (serverda belgilangan)")
        rows = []
        for a in rows_db:
            lines.append(f"👤 {esc(a['name'] or '')} <code>{a['user_id']}</code>")
            rows.append([btn(f"🗑 {a['name'] or a['user_id']}"[:50], f"ad:amdel:{a['user_id']}")])
        lines.append("\nQo'shilgan adminlar boshqa adminlarni tayinlay olmaydi.")
        rows += [[btn("➕ Admin qo'shish", "ad:amadd")], [btn("🔙 Orqaga", "ad")]]
        await show(uid, mid, "\n".join(lines), kb(rows))

    elif sub == "amadd":
        state[uid] = {"s": "amadd"}
        await show(uid, mid, "➕ Yangi adminning <b>Telegram ID</b> raqamini yuboring, yoki uning botga yozgan "
                             "<b>@username</b>ini, yoki undan biror xabarni <b>forward</b> qiling.\n"
                             "⚠️ U avval botga /start bosgan bo'lishi kerak.", kb([[btn("❌ Bekor qilish", "ad:am")]]))

    elif sub == "amdel":
        t_id = int(p[2])
        db.execute("DELETE FROM admins WHERE user_id=?", (t_id,))
        db.commit()
        if t_id not in _adm_list:
            ADMINS.discard(t_id)
            state.pop(t_id, None)
        await on_admin_cb(uid, msg, "ad:am")

    elif sub == "add":
        state[uid] = {"s": "title", "d": {}}
        await show(uid, mid, "➕ <b>Yangi anime</b>\n\n1/4. Anime <b>nomini</b> yozing:",
                   kb([[btn("❌ Bekor qilish", "ad")]]))

    elif sub == "pick":  # ad:pick:<amal>:<sahifa>
        action, page = p[2], int(p[3])
        titles = {"addep": "🎞 Qaysi animega qism qo'shamiz?", "del": "🗑 Qaysi animeni o'chiramiz?",
                  "delep": "✂️ Qaysi animening qismini o'chiramiz?"}
        cb = {"addep": "ad:epa", "del": "ad:dela", "delep": "ad:epl"}[action]
        items = list_items("all", uid)
        if not items:
            await show(uid, mid, "Hozircha anime yo'q.", kb([[btn("🔙 Orqaga", "ad")]]))
            return
        await show(uid, mid, f"<b>{titles[action]}</b>",
                   paged_kb(items, page, lambda i: f"{cb}:{i}", lambda pg: f"ad:pick:{action}:{pg}", back="ad"))

    elif sub == "epa":  # mavjud animega qism qo'shish
        aid = int(p[2])
        a = db.execute("SELECT * FROM anime WHERE id=?", (aid,)).fetchone()
        if not a:
            return
        state[uid] = {"s": "eps", "aid": aid, "n": 0, "new": False}
        await show(uid, mid, f"🎞 <b>{esc(a['title'])}</b>\n\nEndi qismlarni <b>video</b> yoki <b>fayl</b> qilib "
                             "ketma-ket yuboring. Har biri keyingi raqam bilan saqlanadi.",
                   kb([[btn("✅ Tugatish", "ad:done")]]))

    elif sub == "done":
        st = state.pop(uid, None)
        if not st or st.get("s") != "eps":
            await show_admin(uid, mid)
            return
        if st["n"] == 0:
            msg_t = "ℹ️ Qism qo'shilmadi."
        else:
            start_job("anime", "both" if st["new"] else "channels", uid, anime_id=st["aid"],
                      anime_new=int(st["new"]), added=st["n"])
            msg_t = f"✅ Saqlandi. Qo'shilgan qismlar: {st['n']}.\n📣 " + (
                "E'lon hamma foydalanuvchilarga va E'lon kanallariga yuborilmoqda..." if st["new"]
                else "E'lon E'lon kanallariga yuborilmoqda...")
        await show(uid, mid, f"{msg_t}\n🔢 Anime kodi: <code>{st['aid']}</code>", kb([[btn("👑 Admin panel", "ad")]]))

    elif sub == "dela":
        aid = int(p[2])
        a = db.execute("SELECT title FROM anime WHERE id=?", (aid,)).fetchone()
        if a:
            await show(uid, mid, f"❓ <b>{esc(a['title'])}</b> (kod {aid}) va uning barcha qismlari o'chirilsinmi?",
                       kb([[btn("🗑 Ha, o'chirish", f"ad:delok:{aid}"), btn("❌ Yo'q", "ad")]]))

    elif sub == "delok":
        delete_anime(int(p[2]))
        await show(uid, mid, "🗑 O'chirildi.", kb([[btn("🗑 Yana o'chirish", "ad:pick:del:0"), btn("👑 Admin", "ad")]]))

    elif sub == "epl":  # qismlar ro'yxati (o'chirish uchun)
        aid = int(p[2])
        a = db.execute("SELECT title FROM anime WHERE id=?", (aid,)).fetchone()
        nums = [r["num"] for r in db.execute("SELECT num FROM episodes WHERE anime_id=? ORDER BY num", (aid,))]
        if not a:
            return
        rows = [[btn(f"🗑 {n}", f"ad:epd:{aid}:{n}") for n in nums[i : i + 5]] for i in range(0, min(len(nums), 60), 5)]
        rows.append([btn("🔙 Orqaga", "ad:pick:delep:0")])
        await show(uid, mid, f"✂️ <b>{esc(a['title'])}</b>\nO'chiriladigan qismni tanlang:" if nums
                   else "Bu animeda qism yo'q.", kb(rows))

    elif sub == "epd":
        aid, num = int(p[2]), int(p[3])
        db.execute("DELETE FROM episodes WHERE anime_id=? AND num=?", (aid, num))
        db.commit()
        await on_admin_cb(uid, msg, f"ad:epl:{aid}")

    elif sub == "ch":
        chs = channels()
        rows = [[btn(f"🗑 {c['title']}"[:50], f"ad:chdel:{c['id']}")] for c in chs]
        rows.append([btn("➕ Kanal qo'shish", "ad:chadd")])
        rows.append([btn("🔙 Orqaga", "ad")])
        await show(uid, mid, "📢 <b>Majburiy obuna kanallari</b>\n" + (
            "Kanalni o'chirish uchun uning tugmasini bosing." if chs else "\nHozircha kanal yo'q."), kb(rows))

    elif sub == "chadd":
        state[uid] = {"s": "chadd"}
        await show(uid, mid, "➕ Kanalni qo'shish uchun <b>@username</b> yoki havolasini yuboring, yoki kanaldan "
                             "biror postni <b>forward</b> qiling.\n\n⚠️ Bot avval o'sha kanalga <b>admin</b> "
                             "bo'lishi kerak.", kb([[btn("❌ Bekor qilish", "ad:ch")]]))

    elif sub == "chdel":
        db.execute("DELETE FROM channels WHERE id=?", (int(p[2]),))
        db.commit()
        sub_cache.clear()
        await on_admin_cb(uid, msg, "ad:ch")

    elif sub == "news":
        chs = news_channels()
        rows = [[btn(f"🗑 {c['title']}"[:50], f"ad:nd:{c['chat_id']}")] for c in chs]
        rows += [[btn("➕ Kanal qo'shish", "ad:newsset")], [btn("🔙 Orqaga", "ad")]]
        await show(uid, mid, "📣 <b>E'lon kanallari</b>\nYangi anime qo'shilganda va «📨 Xabar yuborish» da «Kanallarga» "
                             "tanlanganda xabar shu kanallarga boradi.\n" + (
                             "Kanalni olib tashlash uchun tugmasini bosing." if chs else "\nHozircha kanal yo'q."), kb(rows))

    elif sub == "newsset":
        state[uid] = {"s": "news"}
        await show(uid, mid, "📣 Kanalning <b>@username</b>ini yuboring yoki undan postni <b>forward</b> qiling.\n"
                             "Bot u yerda <b>admin</b> bo'lib, xabar yubora olishi kerak.",
                   kb([[btn("❌ Bekor qilish", "ad:news")]]))

    elif sub == "nd":
        db.execute("DELETE FROM news_channels WHERE chat_id=?", (int(p[2]),))
        db.commit()
        await on_admin_cb(uid, msg, "ad:news")

    elif sub == "bc":
        state[uid] = {"s": "bc_msg"}
        await show(uid, mid, "📨 <b>Xabar yuborish</b>\n\nYubormoqchi bo'lgan xabaringizni shu yerga yuboring: matn, rasm, "
                             "video, stiker, ovozli xabar, fayl... (hammasi bo'ladi).\n"
                             "⚠️ Albom emas, bittadan yuboring.", kb([[btn("❌ Bekor qilish", "ad")]]))

    elif sub == "bt":  # ad:bt:<users|channels|both>
        st = state.get(uid)
        if not st or st.get("s") != "bc_pick":
            return
        tgt = p[2]
        users_n = db.execute("SELECT COUNT(*) FROM users WHERE blocked=0").fetchone()[0]
        ch_n = len(news_channels())
        if tgt in ("channels", "both") and ch_n == 0:
            await show(uid, mid, "❌ E'lon kanali yo'q. Avval «📣 E'lon kanallari» bo'limida kanal qo'shing "
                                 "yoki «Foydalanuvchilarga» ni tanlang.", target_kb())
            return
        st["target"] = tgt
        await show(uid, mid, f"🎯 Qabul qiluvchi: <b>{TARGET_NAMES[tgt]}</b>\n"
                             f"(👥 {users_n} ta foydalanuvchi, 📣 {ch_n} ta kanal)\n\nQachon yuboramiz?",
                   kb([[btn("🚀 Hozir yuborish", "ad:bw:now")], [btn("📅 Bir marta (sana, vaqt)", "ad:bw:once")],
                       [btn("🔁 Har kuni", "ad:bw:daily"), btn("🗓 Har hafta", "ad:bw:weekly")],
                       [btn("❌ Bekor qilish", "ad")]]))

    elif sub == "bw":  # ad:bw:<now|once|daily|weekly>
        st = state.get(uid)
        if not st or st.get("s") not in ("bc_pick", "bc_dt", "bc_t") or "target" not in st:
            return
        when = p[2]
        cancel = kb([[btn("❌ Bekor qilish", "ad")]])
        if when == "now":
            st["s"] = "bc_pick"
            await show(uid, mid, f"🚀 <b>{TARGET_NAMES[st['target']]}</b> ga hoziroq yuboriladi. Tasdiqlaysizmi?",
                       kb([[btn("✅ Ha, yuborish", "ad:bgo"), btn("❌ Yo'q", "ad")]]))
        elif when == "once":
            st.update(s="bc_dt", kind="once")
            await show(uid, mid, f"📅 Sana va vaqtni yozing (vaqt zonasi UTC+{TZ_HOURS}).\n"
                                 "Masalan: <code>2026-11-20 18:30</code> yoki <code>20.11.2026 18:30</code>", cancel)
        elif when == "daily":
            st.update(s="bc_t", kind="daily")
            await show(uid, mid, f"🔁 Har kuni qaysi vaqtda (UTC+{TZ_HOURS})? Masalan: <code>09:00</code>", cancel)
        elif when == "weekly":
            short = ["Dush", "Sesh", "Chor", "Pay", "Juma", "Shan", "Yak"]
            btns = [btn(n, f"ad:bd:{i}") for i, n in enumerate(short)]
            await show(uid, mid, "🗓 Haftaning qaysi kuni?",
                       kb([btns[:4], btns[4:], [btn("❌ Bekor qilish", "ad")]]))

    elif sub == "bd":  # hafta kuni tanlandi
        st = state.get(uid)
        if not st or "target" not in st:
            return
        st.update(s="bc_t", kind="weekly", weekday=int(p[2]))
        await show(uid, mid, f"🗓 Har {WEEKDAYS[int(p[2])].lower()} qaysi vaqtda (UTC+{TZ_HOURS})? "
                             "Masalan: <code>09:00</code>", kb([[btn("❌ Bekor qilish", "ad")]]))

    elif sub == "bgo":
        st = state.pop(uid, None)
        if not st or "target" not in st or st.get("s") != "bc_pick":
            return
        start_job("copy", st["target"], uid, st["src"][0], st["src"][1])
        await show(uid, mid, "🚀 Yuborish boshlandi. Tugagach hisobot keladi.", kb([[btn("👑 Admin panel", "ad")]]))

    elif sub == "sl":
        items = db.execute("SELECT * FROM sched WHERE active=1 ORDER BY next_run LIMIT 20").fetchall()
        if not items:
            await show(uid, mid, "⏰ Rejalangan xabarlar yo'q.",
                       kb([[btn("📨 Yangi xabar", "ad:bc"), btn("🔙 Orqaga", "ad")]]))
            return
        lines = ["⏰ <b>Rejalangan xabarlar</b>", ""]
        rows = []
        for x in items:
            lines.append(f"#{x['id']} · {sched_desc(x)} · {TARGET_NAMES[x['target']]}\n      ⏭ {x['next_run']}")
            rows.append([btn(f"👁 #{x['id']}", f"ad:sv:{x['id']}"), btn(f"🗑 #{x['id']}", f"ad:sd:{x['id']}")])
        rows.append([btn("📨 Yangi xabar", "ad:bc"), btn("🔙 Orqaga", "ad")])
        await show(uid, mid, "\n".join(lines), kb(rows))

    elif sub == "sv":  # rejalangan xabarni ko'rish
        x = db.execute("SELECT * FROM sched WHERE id=?", (int(p[2]),)).fetchone()
        if x and await safe("copyMessage", chat_id=uid, from_chat_id=x["src_chat"], message_id=x["src_msg"]):
            return
        await send(uid, "❌ Xabar topilmadi (suhbatdan o'chirib yuborilgan bo'lishi mumkin).")

    elif sub == "sd":
        db.execute("UPDATE sched SET active=0 WHERE id=?", (int(p[2]),))
        db.commit()
        await on_admin_cb(uid, msg, "ad:sl")

    elif sub == "us":  # ad:us[:flt[:page]]
        flt = p[2] if len(p) > 2 else ""
        page = int(p[3]) if len(p) > 3 else 0
        if not flt:
            await show(uid, mid, users_summary(), users_filter_kb())
        else:
            await show_users(uid, mid, flt, page)

    elif sub == "wr":  # ad:wr:<user_id> — foydalanuvchiga xabar yozish
        tid = int(p[2])
        r = db.execute("SELECT * FROM users WHERE user_id=?", (tid,)).fetchone()
        if not r:
            await send(uid, "Foydalanuvchi topilmadi.")
            return
        if r["blocked"]:
            await send(uid, "🚫 Bu foydalanuvchi botni bloklagan, xabar yuborib bo'lmaydi.")
            return
        state[uid] = {"s": "wr", "to": tid}
        await send(uid, f"✉️ <b>{esc(r['first_name'] or str(tid))}</b> ga yuboriladigan xabarni yozing "
                        "(matn, rasm, video, stiker, ovoz...). Bekor qilish uchun /start yoki menyu tugmasini bosing.",
                   kb([[btn("❌ Bekor qilish", "ad")]]))

    elif sub == "uv":  # ad:uv:<id>:<flt>:<page>
        await show_user_card(uid, mid, int(p[2]), p[3] if len(p) > 3 else "all", int(p[4]) if len(p) > 4 else 0)

    elif sub == "stat":
        await show(uid, mid, stats_text(), kb([[btn("🔄 Yangilash", "ad:stat"), btn("🔙 Orqaga", "ad")]]))


# ---------- admin: matnli bosqichlar ----------
def media_of(msg: dict):
    if msg.get("video"):
        return msg["video"]["file_id"], "video"
    if msg.get("animation"):
        return msg["animation"]["file_id"], "document"
    if msg.get("document"):
        return msg["document"]["file_id"], "document"
    return None


async def admin_state(uid: int, msg: dict) -> bool:
    st = state[uid]
    s = st["s"]
    text = (msg.get("text") or "").strip()
    if text.startswith("/"):
        state.pop(uid, None)
        return False

    if s == "title":
        if not text:
            await send(uid, "Nomni matn qilib yozing:")
            return True
        st["d"]["title"] = text[:200]
        st["s"] = "desc"
        await send(uid, "2/4. <b>Tavsif</b> yozing (yoki <code>-</code> yuboring):")
    elif s == "desc":
        st["d"]["descr"] = None if text in ("", "-") else text[:1500]
        st["s"] = "genre"
        await send(uid, "3/4. <b>Janr</b> yozing, masalan: <i>Jangari, Sarguzasht</i> (yoki <code>-</code>):")
    elif s == "genre":
        st["d"]["genre"] = None if text in ("", "-") else text[:100]
        st["s"] = "poster"
        await send(uid, "4/4. <b>Poster rasmini</b> yuboring (yoki <code>-</code>):")
    elif s == "poster":
        photo = (msg.get("photo") or [None])[-1]
        if not photo and text != "-":
            await send(uid, "Rasm yuboring yoki <code>-</code> yozing:")
            return True
        d = st["d"]
        cur = db.execute("INSERT INTO anime(title,descr,genre,poster,created) VALUES(?,?,?,?,?)",
                         (d["title"], d.get("descr"), d.get("genre"), photo["file_id"] if photo else None, today()))
        db.commit()
        aid = cur.lastrowid
        state[uid] = {"s": "eps", "aid": aid, "n": 0, "new": True}
        await send(uid, f"✅ Anime yaratildi. 🔢 Kodi: <code>{aid}</code>\n\nEndi qismlarni <b>video</b> yoki "
                        "<b>fayl</b> qilib ketma-ket yuboring (1-qismdan boshlab). Tugagach "
                        "<b>✅ Tugatish</b> ni bosing.", kb([[btn("✅ Tugatish", "ad:done")]]))
    elif s == "eps":
        media = media_of(msg)
        if not media:
            await send(uid, "Video yoki fayl yuboring. Tugatish uchun tugmani bosing:",
                       kb([[btn("✅ Tugatish", "ad:done")]]))
            return True
        num = (db.execute("SELECT MAX(num) FROM episodes WHERE anime_id=?", (st["aid"],)).fetchone()[0] or 0) + 1
        db.execute("INSERT INTO episodes(anime_id,num,file_id,ftype) VALUES(?,?,?,?)", (st["aid"], num, *media))
        db.commit()
        st["n"] += 1
        await send(uid, f"✅ {num}-qism saqlandi.", kb([[btn("✅ Tugatish", "ad:done")]]))
    elif s == "wr":
        if not text and not any(k in msg for k in (
                "photo", "video", "sticker", "voice", "audio", "document", "animation", "video_note", "poll")):
            await send(uid, "Yuboriladigan xabarni yozing:")
            return True
        tid = st["to"]
        state.pop(uid, None)
        try:
            await tg("copyMessage", chat_id=tid, from_chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                     reply_markup=kb([[btn("✍️ Javob yozish", "uc:r"), btn("❌ Suhbatni tugatish", "uc:x")]]))
            db.execute("UPDATE users SET chat_admin=?, chat_until=? WHERE user_id=?", (uid, chat_deadline(), tid))
            db.commit()
            await send(uid, "✅ Yuborildi. U javob yozsa, shu yerga keladi.", kb([[btn("✉️ Yana yozish", f"ad:wr:{tid}")]]))
        except TGError as e:
            if e.code == 403:
                db.execute("UPDATE users SET blocked=1 WHERE user_id=?", (tid,))
                db.commit()
                await send(uid, "🚫 Foydalanuvchi botni bloklagan, yuborilmadi.")
            else:
                await send(uid, f"❌ Yuborilmadi: {esc(str(e))}")
    elif s == "amadd":
        if uid != MAIN_ADMIN:
            state.pop(uid, None)
            return True
        new_id, name = None, ""
        ff = msg.get("forward_from") or ((msg.get("forward_origin") or {}).get("sender_user"))
        if ff:
            new_id, name = ff["id"], ff.get("first_name", "")
        elif text.lstrip("-").isdigit():
            new_id = int(text)
        elif text.startswith("@"):
            r = db.execute("SELECT user_id, first_name FROM users WHERE lower(username)=?", (text[1:].lower(),)).fetchone()
            if r:
                new_id, name = r["user_id"], r["first_name"]
        if not new_id:
            await send(uid, "❌ Topilmadi. ID raqam yuboring, yoki u botga /start bosganini tekshirib @username yozing, "
                            "yoki undan xabar forward qiling:")
            return True
        r = db.execute("SELECT first_name FROM users WHERE user_id=?", (new_id,)).fetchone()
        if not r:
            await send(uid, "❌ Bu foydalanuvchi botga hali /start bosmagan. Avval u botni ochsin, keyin qayta urining.")
            return True
        name = name or r["first_name"] or ""
        state.pop(uid, None)
        if new_id in ADMINS:
            await send(uid, "ℹ️ Bu foydalanuvchi allaqachon admin.", kb([[btn("👥 Adminlar", "ad:am")]]))
            return True
        db.execute("INSERT OR REPLACE INTO admins(user_id,name) VALUES(?,?)", (new_id, name))
        db.commit()
        ADMINS.add(new_id)
        await send(uid, f"✅ Admin qo'shildi: <b>{esc(name)}</b> (<code>{new_id}</code>)", kb([[btn("👥 Adminlar", "ad:am")]]))
        await safe("sendMessage", chat_id=new_id, text="👑 Siz botga admin qilindingiz. /start bosing.",
                   parse_mode="HTML")
    elif s == "bc_msg":
        if not text and not any(k in msg for k in (
                "photo", "video", "sticker", "voice", "audio", "document", "animation", "video_note", "poll")):
            await send(uid, "Yubormoqchi bo'lgan xabaringizni yuboring:")
            return True
        mg = msg.get("media_group_id")
        state[uid] = {"s": "bc_pick", "src": (msg["chat"]["id"], msg["message_id"]), "mg": mg}
        note = "\n⚠️ Albom aniqlandi: faqat birinchi fayl yuboriladi." if mg else ""
        await send(uid, "✅ Xabar qabul qilindi." + note + "\n\nKimga yuboramiz?", target_kb())
    elif s == "bc_pick":
        if msg.get("media_group_id") and msg.get("media_group_id") == st.get("mg"):
            return True  # albomning qolgan qismlari
        await send(uid, "Yuqoridagi tugmalardan birini tanlang yoki «❌ Bekor qilish» ni bosing.")
    elif s in ("bc_dt", "bc_t"):
        try:
            if s == "bc_dt":
                dt = parse_dt(text)
                if dt <= naive_now():
                    raise ValueError("Bu vaqt o'tib ketgan. Kelajakdagi vaqtni yozing.")
            else:
                hh, mm = parse_hm(text)
                dt = first_run(st["kind"], hh, mm, st.get("weekday"))
        except ValueError as e:
            await send(uid, f"❌ {e}")
            return True
        sid = add_sched(uid, st, dt)
        state.pop(uid, None)
        row = db.execute("SELECT * FROM sched WHERE id=?", (sid,)).fetchone()
        await send(uid, f"✅ <b>Rejalashtirildi</b> (#{sid})\n{sched_desc(row)}\n🎯 {TARGET_NAMES[row['target']]}\n"
                        f"⏭ Keyingi yuborish: <b>{row['next_run']}</b>\n\n"
                        "⚠️ Rejalangan xabarni shu suhbatdan o'chirmang, aks holda yuborilmaydi.",
                   kb([[btn("⏰ Rejalangan xabarlar", "ad:sl")]]))
    elif s in ("chadd", "news"):
        ref = chat_ref(msg)
        if ref is None:
            await send(uid, "Kanal @username, havola yoki forward qilingan post yuboring:")
            return True
        try:
            info = await verify_channel(ref, need_post=(s == "news"))
        except ValueError as e:
            await send(uid, str(e))
            return True
        state.pop(uid, None)
        if s == "chadd":
            db.execute("INSERT OR REPLACE INTO channels(chat_id,title,link) VALUES(?,?,?)",
                       (info["chat_id"], info["title"], info["link"]))
            db.commit()
            sub_cache.clear()
            await send(uid, f"✅ Majburiy kanal qo'shildi: <b>{esc(info['title'])}</b>", kb([[btn("📢 Kanallar", "ad:ch")]]))
        else:
            db.execute("INSERT OR REPLACE INTO news_channels(chat_id,title) VALUES(?,?)", (info["chat_id"], info["title"]))
            db.commit()
            await send(uid, f"✅ E'lon kanali qo'shildi: <b>{esc(info['title'])}</b>", kb([[btn("📣 Kanallar", "ad:news")]]))
    return True


# ---------- foydalanuvchi xabarlari ----------
async def after_gate(uid: int, payload: str) -> None:
    if payload.startswith("a") and payload[1:].isdigit():
        await send_card(uid, int(payload[1:]))
    else:
        await show_menu(uid)


async def on_message(msg: dict) -> None:
    if msg["chat"]["type"] != "private" or "from" not in msg:
        return
    uid = msg["from"]["id"]
    new_user = reg_user(msg["from"])
    text = (msg.get("text") or "").strip()

    if new_user and uid not in ADMINS:
        spawn(notify_main(uid, "🆕 <b>Yangi foydalanuvchi botga /start bosdi</b>"))

    ct = msg.get("contact")
    if ct:
        if ct.get("user_id") == uid and ct.get("phone_number"):
            ph = ct["phone_number"]
            first = not has_phone(uid)
            db.execute("UPDATE users SET phone=? WHERE user_id=?", (ph if ph.startswith("+") else "+" + ph, uid))
            db.commit()
            if first:
                spawn(notify_main(uid, "✅ <b>Ro'yxatdan o'tdi (raqam berdi)</b>"))
            await send(uid, "✅ Ro'yxatdan o'tdingiz, rahmat!", main_menu(uid))
            if not await gate(uid):
                await after_gate(uid, pending.pop(uid, ""))
        else:
            await gate(uid)
        return

    if uid in ADMINS and uid in state:
        if text in ALL_BTN_TEXTS:  # menyu tugmasi bosilsa, joriy bosqich bekor bo'ladi
            state.pop(uid, None)
        elif await admin_state(uid, msg):
            return

    if text.startswith("/start"):
        payload = text.split(maxsplit=1)[1].strip() if " " in text else ""
        state.pop(uid, None)
        if await gate(uid, payload or "-"):
            return
        await after_gate(uid, payload)
        return
    if await gate(uid):
        return
    if uid not in ADMINS and chat_open(uid) and not (
            text in USER_BTN_REV or text == B_MENU or text.startswith("/") or text.isdigit()):
        await to_admin(uid, msg)
        return
    if text in USER_BTN_REV:
        await show_list(uid, None, USER_BTN_REV[text], 0)
    elif text == B_MENU:
        await show_menu(uid)
    elif uid in ADMINS and text == B_ADMIN:
        await show_admin(uid)
    elif uid in ADMINS and text in ADMIN_BTN_REV:
        await on_admin_cb(uid, {}, ADMIN_BTN_REV[text])
    elif text.startswith("/admin") and uid in ADMINS:
        await show_admin(uid)
    elif text.startswith("/menu") or text.startswith("/"):
        await show_menu(uid)
    elif text.isdigit():
        await send_card(uid, int(text))
    elif text:
        like = f"%{text.replace('%', '').replace('_', '')[:50]}%"
        rows = db.execute("SELECT id, title FROM anime WHERE title LIKE ? COLLATE NOCASE ORDER BY id DESC LIMIT 10",
                          (like,)).fetchall()
        if not rows:
            await send(uid, "🔍 Hech narsa topilmadi. Kodni yoki boshqa nomni yozib ko'ring.",
                       kb([[btn("🏠 Menyu", "menu")]]))
        else:
            await send(uid, "🔍 <b>Topildi:</b>", kb([[btn(r["title"][:60], f"a:{r['id']}")] for r in rows]
                                                    + [[btn("🏠 Menyu", "menu")]]))


async def on_callback(cb: dict) -> None:
    uid = cb["from"]["id"]
    if reg_user(cb["from"]) and uid not in ADMINS:
        spawn(notify_main(uid, "🆕 <b>Yangi foydalanuvchi botga kirdi</b>"))
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    mid = msg.get("message_id")
    p = data.split(":")
    cmd = p[0]

    async def answer(text: str = "", alert: bool = False) -> None:
        await safe("answerCallbackQuery", callback_query_id=cb["id"], text=text, show_alert=alert)

    if cmd == "noop":
        await answer()
        return
    if cmd == "ad":
        await answer()
        if uid in ADMINS:
            await on_admin_cb(uid, msg, data)
        return
    if cmd == "chk":
        sub_cache.pop(uid, None)
        if await missing_channels(uid):
            await answer("❌ Hali hamma kanalga obuna bo'lmagansiz.", True)
            return
        await answer("✅ Rahmat!")
        if mid:
            await safe("deleteMessage", chat_id=uid, message_id=mid)
        pl = "" if len(p) < 2 or p[1] == "-" else p[1]
        if not await gate(uid, pl or "-"):
            await after_gate(uid, pl)
        return

    if cmd == "uc":  # foydalanuvchi suhbati: uc:r (javob yozish), uc:x (tugatish)
        if p[1] == "x":
            db.execute("UPDATE users SET chat_until=NULL WHERE user_id=?", (uid,))
            db.commit()
            await answer("Suhbat tugatildi")
        else:
            db.execute("UPDATE users SET chat_until=? WHERE user_id=?", (chat_deadline(), uid))
            db.commit()
            await answer()
            await send(uid, "✍️ Xabaringizni yozing (matn, rasm, ovoz...), adminga yetkazaman.")
        return

    if await gate(uid):
        await answer()
        return

    if cmd == "rs":  # baholash
        aid, score = int(p[1]), int(p[2])
        if 1 <= score <= 5 and db.execute("SELECT 1 FROM anime WHERE id=?", (aid,)).fetchone():
            db.execute("INSERT OR REPLACE INTO ratings(user_id,anime_id,score) VALUES(?,?,?)", (uid, aid, score))
            db.commit()
        await answer(f"Bahoyingiz: {score}⭐")
        await refresh_card(msg, aid, uid)
        return
    if cmd == "f":  # saqlash / olib tashlash
        aid = int(p[1])
        if db.execute("SELECT 1 FROM favorites WHERE user_id=? AND anime_id=?", (uid, aid)).fetchone():
            db.execute("DELETE FROM favorites WHERE user_id=? AND anime_id=?", (uid, aid))
            await answer("Saqlanganlardan olib tashlandi")
        else:
            db.execute("INSERT OR IGNORE INTO favorites(user_id,anime_id) VALUES(?,?)", (uid, aid))
            await answer("💾 Saqlandi")
        db.commit()
        await refresh_card(msg, aid, uid)
        return

    await answer()
    if cmd == "menu":
        await show_menu(uid, None if (msg.get("photo") or msg.get("video") or msg.get("document")) else mid)
    elif cmd == "a":
        await send_card(uid, int(p[1]))
    elif cmd == "e":
        await send_episode(uid, int(p[1]), int(p[2]))
    elif cmd == "pg":
        await refresh_card(msg, int(p[1]), uid, int(p[2]))
    elif cmd == "r":
        await refresh_card(msg, int(p[1]), uid, 0, True)
    elif cmd == "l":
        await show_list(uid, mid, p[1], int(p[2]))


async def handle_update(u: dict) -> None:
    if "callback_query" in u:
        await on_callback(u["callback_query"])
    elif "message" in u:
        await on_message(u["message"])
    elif "my_chat_member" in u:
        mc = u["my_chat_member"]
        if mc["chat"]["type"] == "private":
            st = mc["new_chat_member"]["status"]
            db.execute("UPDATE users SET blocked=? WHERE user_id=?", (1 if st == "kicked" else 0, mc["chat"]["id"]))
            db.commit()


locks: dict[int, asyncio.Lock] = {}
tasks: set = set()


def update_key(u: dict) -> int:
    for k in ("callback_query", "message", "my_chat_member"):
        if k in u and "from" in u[k]:
            return u[k]["from"]["id"]
    return 0


async def handle_safe(u: dict) -> None:
    lock = locks.setdefault(update_key(u), asyncio.Lock())
    async with lock:
        try:
            await handle_update(u)
        except Exception as e:  # noqa: BLE001
            log.error("update xato: %r", e)


# ---------- zaxira nusxa ----------
_bk_changes = -1
_bk_msg = 0


async def backup_db(force: bool = False) -> None:
    global _bk_changes, _bk_msg
    if not BACKUP_ON or not BACKUP_CHAT:
        return
    if db.total_changes == _bk_changes and not force:
        return
    try:
        db.commit()
        tmp = DB_PATH + ".bak"
        dst = sqlite3.connect(tmp)
        db.backup(dst)
        dst.close()
        with open(tmp, "rb") as f:
            data = f.read()
        r = await http.post(
            f"{API}/sendDocument",
            data={"chat_id": BACKUP_CHAT, "caption": "💾 anime.db zaxira (o'chirmang)", "disable_notification": "true"},
            files={"document": (BACKUP_NAME, data)},
            timeout=120,
        )
        res = r.json()
        if not res.get("ok"):
            raise RuntimeError(res.get("description"))
        mid = res["result"]["message_id"]
        await tg("pinChatMessage", chat_id=BACKUP_CHAT, message_id=mid, disable_notification=True)
        if _bk_msg:
            await safe("deleteMessage", chat_id=BACKUP_CHAT, message_id=_bk_msg)
        _bk_msg = mid
        _bk_changes = db.total_changes
        log.info("Zaxira yuborildi (%d bayt)", len(data))
    except Exception as e:  # noqa: BLE001
        log.error("zaxira xato: %r", e)


async def backup_loop() -> None:
    while True:
        await asyncio.sleep(BACKUP_EVERY)
        await backup_db()


# ---------- webhook server ----------
async def web_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    status, body, method = 404, b"not found", ""
    try:
        line = await asyncio.wait_for(reader.readline(), 15)
        parts = line.decode("latin1").split()
        if len(parts) < 2:
            return
        method, path = parts[0], parts[1].split("?")[0]
        headers: dict[str, str] = {}
        while True:
            h = await asyncio.wait_for(reader.readline(), 15)
            if h in (b"\r\n", b"\n", b""):
                break
            k, _, v = h.decode("latin1").partition(":")
            headers[k.strip().lower()] = v.strip()
        if method in ("GET", "HEAD") and path in ("/", "/health", "/healthz"):
            status, body = 200, b"ok"
        elif method == "POST" and path == WEBHOOK_PATH:
            n = int(headers.get("content-length", "0") or 0)
            if headers.get("x-telegram-bot-api-secret-token") != WEBHOOK_SECRET:
                status, body = 403, b"forbidden"
            elif n <= 0 or n > 2_000_000:
                status, body = 400, b"bad request"
            else:
                u = json.loads(await asyncio.wait_for(reader.readexactly(n), 15))
                t = asyncio.create_task(handle_safe(u))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
                status, body = 200, b"ok"
    except Exception as e:  # noqa: BLE001
        log.error("web: %r", e)
        status, body = 400, b"error"
    finally:
        try:
            reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found"}.get(status, "")
            head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
            writer.write(head.encode() + (b"" if method == "HEAD" else body))
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        writer.close()


ALLOWED = ["message", "callback_query", "my_chat_member"]


async def serve_webhook() -> None:
    server = await asyncio.start_server(web_conn, "0.0.0.0", PORT)
    await tg("setWebhook", url=f"{WEBHOOK_BASE}{WEBHOOK_PATH}", secret_token=WEBHOOK_SECRET,
             allowed_updates=ALLOWED, max_connections=20)
    log.info("Webhook o'rnatildi: %s%s (port %s)", WEBHOOK_BASE, WEBHOOK_PATH, PORT)
    async with server:
        await server.serve_forever()


async def poll() -> None:
    offset = 0
    while True:
        try:
            updates = await tg("getUpdates", offset=offset, timeout=30, allowed_updates=ALLOWED)
            for u in updates:
                offset = u["update_id"] + 1
                t = asyncio.create_task(handle_safe(u))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
        except Exception as e:  # noqa: BLE001
            log.error("poll (internet uzilgan bo'lishi mumkin): %r", e)
            await asyncio.sleep(5)


async def shutdown() -> None:
    log.info("SIGTERM: zaxira olinmoqda")
    await backup_db(force=True)
    os._exit(0)


async def main() -> None:
    global http, BOT_ID, BOT_USERNAME
    if not ADMINS:
        raise SystemExit("ADMIN_IDS ni yozing (o'z Telegram ID raqamingiz)")
    http = httpx.AsyncClient()
    me = await tg("getMe")
    BOT_ID, BOT_USERNAME = me["id"], me["username"]
    await safe("setMyCommands", commands=[{"command": "start", "description": "Boshlash"},
                                          {"command": "menu", "description": "Asosiy menyu"}])
    old = get_set("news_channel")  # eski versiyadagi yagona E'lon kanalini yangi ro'yxatga ko'chirish
    if old and not news_channels():
        title = old
        try:
            title = (await tg("getChat", chat_id=int(old))).get("title") or old
        except Exception:  # noqa: BLE001
            pass
        db.execute("INSERT OR IGNORE INTO news_channels(chat_id,title) VALUES(?,?)", (int(old), title))
        db.commit()
        set_set("news_channel", "")
    for a in db.execute("SELECT user_id FROM admins").fetchall():
        ADMINS.add(a["user_id"])
    resume_jobs()
    log.info("Anime bot ishga tushdi: @%s | rejim: %s", BOT_USERNAME, "webhook" if WEBHOOK_MODE else "polling")
    if WEBHOOK_MODE:
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(shutdown()))
        except NotImplementedError:
            pass
        await asyncio.gather(serve_webhook(), backup_loop(), scheduler())
    else:
        await tg("deleteWebhook")
        await asyncio.gather(poll(), backup_loop(), scheduler())


if __name__ == "__main__":
    asyncio.run(main())
