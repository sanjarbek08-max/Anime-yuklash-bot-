"""
Bitta bepul serverda ikkita botni ishlatish.

Render bepul rejasida butun akkaunt uchun oyiga 750 soat bor, shuning uchun ikkita alohida
servis 24/7 ishlasa ikkalasi ham to'xtab qoladi. Bu fayl:
  - bot.py (kanal boti)        -> /webhook
  - anime_bot.py (anime boti)  -> /webhook-anime
ni alohida jarayon sifatida ishga tushiradi va bitta portda (Render bergan PORT) so'rovlarni yo'naltiradi.

Muhit o'zgaruvchilari: BOT_TOKEN (kanal boti), ANIME_BOT_TOKEN (anime boti),
ADMIN_IDS, ixtiyoriy ANIME_ADMIN_IDS. Biri bo'lmasa, faqat ikkinchisi ishlaydi.
Start Command:  python run_all.py
"""
import asyncio
import logging
import os
import signal
import sys

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s run_all: %(message)s")
log = logging.getLogger("run_all")

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "10000"))
BASE = os.environ.copy()

BOTS = []  # (nom, fayl, port, yo'l, qo'shimcha env)
if BASE.get("BOT_TOKEN"):
    BOTS.append(("kanal", "bot.py", 10001, "/webhook", {"PORT": "10001", "DB_PATH": "bot.db"}))
if BASE.get("ANIME_BOT_TOKEN"):
    BOTS.append(("anime", "anime_bot.py", 10002, "/webhook-anime", {
        "BOT_TOKEN": BASE["ANIME_BOT_TOKEN"],
        "ADMIN_IDS": BASE.get("ANIME_ADMIN_IDS") or BASE.get("ADMIN_IDS", ""),
        "PORT": "10002", "DB_PATH": "anime.db", "WEBHOOK_PATH": "/webhook-anime",
    }))

procs: dict[str, asyncio.subprocess.Process] = {}
stopping = False
client: httpx.AsyncClient


async def supervise(name: str, script: str, extra: dict) -> None:
    env = BASE.copy()
    env.pop("WEBHOOK_SECRET", None)  # har bot o'z tokenidan maxfiy kalit hosil qiladi
    env.update(extra)
    while not stopping:
        p = await asyncio.create_subprocess_exec(sys.executable, os.path.join(HERE, script), env=env, cwd=HERE)
        procs[name] = p
        log.info("%s boti ishga tushdi (pid %s)", name, p.pid)
        code = await p.wait()
        if stopping:
            return
        log.error("%s boti to'xtadi (kod %s), 5 soniyadan keyin qayta ishga tushadi", name, code)
        await asyncio.sleep(5)


async def route(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
        elif method == "POST":
            target = next((b for b in BOTS if b[3] == path), None)
            n = int(headers.get("content-length", "0") or 0)
            if not target:
                status, body = 404, b"not found"
            elif n <= 0 or n > 2_000_000:
                status, body = 400, b"bad request"
            else:
                raw = await asyncio.wait_for(reader.readexactly(n), 15)
                fwd = {"Content-Type": "application/json"}
                if "x-telegram-bot-api-secret-token" in headers:
                    fwd["X-Telegram-Bot-Api-Secret-Token"] = headers["x-telegram-bot-api-secret-token"]
                try:
                    r = await client.post(f"http://127.0.0.1:{target[2]}{path}", content=raw, headers=fwd, timeout=20)
                    status, body = r.status_code, r.content
                except Exception:  # noqa: BLE001
                    status, body = 502, b"bot is starting"  # Telegram keyinroq qayta yuboradi
    except Exception as e:  # noqa: BLE001
        log.error("route: %r", e)
        status, body = 400, b"error"
    finally:
        try:
            reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 502: "Bad Gateway"}.get(status, "")
            head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
            writer.write(head.encode() + (b"" if method == "HEAD" else body))
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        writer.close()


async def stop_all() -> None:
    global stopping
    stopping = True
    log.info("SIGTERM: botlar to'xtatilmoqda (zaxira olinadi)")
    for p in procs.values():
        if p.returncode is None:
            p.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.gather(*[p.wait() for p in procs.values()]), 25)
    except asyncio.TimeoutError:
        pass
    os._exit(0)


async def main() -> None:
    global client
    if not BOTS:
        raise SystemExit("BOT_TOKEN yoki ANIME_BOT_TOKEN kerak")
    client = httpx.AsyncClient()
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(stop_all()))
    except NotImplementedError:
        pass
    server = await asyncio.start_server(route, "0.0.0.0", PORT)
    log.info("Yo'naltirgich port %s da: %s", PORT, ", ".join(f"{b[0]}{b[3]}" for b in BOTS))
    sups = [supervise(n, f, e) for n, f, _, _, e in BOTS]
    async with server:
        await asyncio.gather(server.serve_forever(), *sups)


if __name__ == "__main__":
    asyncio.run(main())
