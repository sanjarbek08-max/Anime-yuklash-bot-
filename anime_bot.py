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
"""
)
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


esc = html.escape


# ---------- Telegram API ----------
async def tg(method: str, **params):
    r = await http.post(f"{API}/{method}", json=params, timeout=70)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or f"{method} xato")
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
def reg_user(u: dict) -> None:
    uid = u["id"]
    name = u.get("first_name") or ""
    r = db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone()
    if r:
        db.execute("UPDATE users SET first_name=?, username=?, last_seen=?, blocked=0 WHERE user_id=?",
                   (name, u.get("username") or "", today(), uid))
    else:
        db.execute("INSERT INTO users(user_id,first_name,username,joined,last_seen) VALUES(?,?,?,?,?)",
                   (uid, name, u.get("username") or "", today(), today()))
    db.commit()


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


async def gate(uid: int, payload: str = "-") -> bool:
    """Obuna bo'lmagan bo'lsa kanallar ro'yxatini yuboradi va True qaytaradi."""
    miss = await missing_channels(uid)
    if not miss:
        return False
    rows = [[ubtn(f"📢 {c['title']}", c["link"])] for c in miss]
    rows.append([btn("✅ Tekshirish", f"chk:{payload}")])
    await send(uid, "Botdan foydalanish uchun avval quyidagi kanallarga obuna bo'ling, so'ng "
                    "<b>✅ Tekshirish</b> tugmasini bosing:", kb(rows))
    return True


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
             "ad:ch": "📢 Majburiy kanallar", "ad:news": "📣 E'lon kanali", "ad:stat": "📊 Statistika"}
ADMIN_BTN_REV = {v: k for k, v in ADMIN_BTN.items()}
ALL_BTN_TEXTS = set(USER_BTN.values()) | set(ADMIN_BTN.values()) | {B_ADMIN, B_MENU}


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


def admin_kb() -> dict:
    t = ADMIN_BTN
    return rkb([[t["ad:add"], t["ad:pick:addep:0"]], [t["ad:pick:del:0"], t["ad:pick:delep:0"]],
                [t["ad:ch"], t["ad:news"]], [t["ad:stat"], B_MENU]])


async def show_admin(uid: int, mid: int | None = None) -> None:
    if mid:
        await safe("deleteMessage", chat_id=uid, message_id=mid)
    await send(uid, "👑 <b>Admin panel</b>\nTugmalar pastda.", admin_kb())


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


async def announce(aid: int, new: bool, added: int) -> bool:
    cid = get_set("news_channel")
    if not cid:
        return False
    a = db.execute("SELECT * FROM anime WHERE id=?", (aid,)).fetchone()
    if not a:
        return False
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
    try:
        if a["poster"]:
            await tg("sendPhoto", chat_id=int(cid), photo=a["poster"], caption=text[:1020], parse_mode="HTML",
                     reply_markup=markup)
        else:
            await send(int(cid), text, markup)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("e'lon yuborilmadi: %s", e)
        return False


def delete_anime(aid: int) -> None:
    for t in ("episodes", "ratings", "favorites", "views"):
        db.execute(f"DELETE FROM {t} WHERE anime_id=?", (aid,))
    db.execute("DELETE FROM anime WHERE id=?", (aid,))
    db.commit()


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
    if sub != "done":
        state.pop(uid, None)

    if sub == "":
        await show_admin(uid, mid)

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
            ok = await announce(st["aid"], st["new"], st["n"])
            msg_t = f"✅ Saqlandi. Qo'shilgan qismlar: {st['n']}.\n" + (
                "📣 Kanalga e'lon yuborildi." if ok else "📣 E'lon yuborilmadi (E'lon kanali sozlanmagan yoki xato).")
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
        cid = get_set("news_channel")
        cur = f"Hozirgi: <code>{cid}</code>" if cid else "Hozircha o'rnatilmagan."
        rows = [[btn("✏️ O'rnatish / almashtirish", "ad:newsset")]]
        if cid:
            rows.append([btn("🗑 O'chirish", "ad:newsoff")])
        rows.append([btn("🔙 Orqaga", "ad")])
        await show(uid, mid, f"📣 <b>E'lon kanali</b>\nYangi anime/qism qo'shilganda shu kanalga xabar boradi.\n\n{cur}",
                   kb(rows))

    elif sub == "newsset":
        state[uid] = {"s": "news"}
        await show(uid, mid, "📣 E'lon kanalining <b>@username</b>ini yuboring yoki undan postni <b>forward</b> qiling.\n"
                             "Bot u yerda <b>admin</b> bo'lishi kerak.", kb([[btn("❌ Bekor qilish", "ad:news")]]))

    elif sub == "newsoff":
        set_set("news_channel", "")
        await on_admin_cb(uid, msg, "ad:news")

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
            set_set("news_channel", str(info["chat_id"]))
            await send(uid, f"✅ E'lon kanali o'rnatildi: <b>{esc(info['title'])}</b>", kb([[btn("👑 Admin", "ad")]]))
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
    reg_user(msg["from"])
    text = (msg.get("text") or "").strip()

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
    reg_user(cb["from"])
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
        await after_gate(uid, "" if len(p) < 2 or p[1] == "-" else p[1])
        return

    if await missing_channels(uid):
        await answer()
        await gate(uid)
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
    log.info("Anime bot ishga tushdi: @%s | rejim: %s", BOT_USERNAME, "webhook" if WEBHOOK_MODE else "polling")
    if WEBHOOK_MODE:
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(shutdown()))
        except NotImplementedError:
            pass
        await asyncio.gather(serve_webhook(), backup_loop())
    else:
        await tg("deleteWebhook")
        await asyncio.gather(poll(), backup_loop())


if __name__ == "__main__":
    asyncio.run(main())
