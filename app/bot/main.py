from __future__ import annotations
import os, hmac, time, asyncio, logging, aiohttp
from hashlib import sha256
from urllib.parse import urlparse, quote_plus

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    KeyboardButton, ReplyKeyboardMarkup
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

from .storage import (
    init_db, migrate, save_event, fetch_geojson,
    add_photo_to_event, delete_event_by_owner,
    upsert_live_event, update_live_coords, stop_live
)

# ========== ENV ==========
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CENTER_LAT = float(os.getenv("CENTER_LAT", "42.179"))
CENTER_LON = float(os.getenv("CENTER_LON", "18.942"))
CENTER_ZOOM = int(os.getenv("CENTER_ZOOM", "12"))
BASE_URL = os.getenv("BASE_URL", "").strip()
MAP_URL = os.getenv("MAP_URL", "").strip()
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me").encode()
PORT = int(os.getenv("PORT", "8080"))  # Railway

WEBMAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "webmap"))

# ========== LOG ==========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("app")

# ========== HELPERS ==========
def _is_public_http(url: str) -> bool:
    if not url:
        return False
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            return False
        host = (u.hostname or "").lower()
        if host in ("localhost", "127.0.0.1"):
            return False
        return True
    except Exception:
        return False

if not _is_public_http(MAP_URL) and _is_public_http(BASE_URL):
    MAP_URL = BASE_URL

def sign_uid(uid: int) -> str:
    return hmac.new(SECRET_KEY, str(uid).encode(), sha256).hexdigest()

def check_sig(uid: int, sig: str) -> bool:
    return hmac.compare_digest(sign_uid(uid), sig or "")

def _read_template(name: str) -> str:
    with open(os.path.join(WEBMAP_DIR, name), "r", encoding="utf-8") as f:
        return f.read()

def user_map_url(user_id: int | None) -> str | None:
    base = MAP_URL if _is_public_http(MAP_URL) else (BASE_URL if _is_public_http(BASE_URL) else None)
    if not base:
        return None
    if user_id is None:
        return base
    return f"{base}?uid={user_id}&sig={sign_uid(user_id)}"

def _map_button(user_id: int | None = None) -> InlineKeyboardMarkup | None:
    url = user_map_url(user_id)
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🌍 View Live Map", url=url)]]
    )

# ========== FASTAPI ==========
app = FastAPI(title="Wildfire MVP")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

@app.on_event("startup")
async def _on_startup():
    init_db()
    migrate()
    log.info("DB ready")

@app.get("/", response_class=HTMLResponse)
def index():
    html = _read_template("index.html")
    html = (html
            .replace("__LAT__", str(CENTER_LAT))
            .replace("__LON__", str(CENTER_LON))
            .replace("__ZOOM__", str(CENTER_ZOOM)))
    return HTMLResponse(html)

@app.get("/pick", response_class=HTMLResponse)
def pick(
    request: Request,
    lat: float = CENTER_LAT,
    lon: float = CENTER_LON,
    z: int = CENTER_ZOOM,
    mode: str = "vol",
    contact: str = ""
):
    html = _read_template("pick.html")
    html = (html
            .replace("__LAT__", str(lat))
            .replace("__LON__", str(lon))
            .replace("__ZOOM__", str(z))
            .replace("__MODE__", "fire" if mode.lower() == "fire" else "vol")
            .replace("__CONTACT__", contact))
    return HTMLResponse(html)

@app.get("/geojson")
def geojson():
    return JSONResponse(fetch_geojson())

@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True})

@app.delete("/event/{event_id}")
def delete_event(event_id: int, uid: int, sig: str):
    if not check_sig(uid, sig):
        raise HTTPException(status_code=403, detail="bad signature")
    ok = delete_event_by_owner(event_id, uid)
    if not ok:
        raise HTTPException(status_code=404, detail="not found or not owner")
    return JSONResponse({"deleted": True, "id": event_id})

# Telegram photo proxy
@app.get("/photo/{file_id}")
async def photo(file_id: str):
    if not TELEGRAM_TOKEN:
        raise HTTPException(status_code=404, detail="bot not configured")
    try:
        async with aiohttp.ClientSession() as s:
            get_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            async with s.get(get_url) as r:
                data = await r.json()
                if not data.get("ok"):
                    raise HTTPException(status_code=404, detail="photo not found")
                file_path = data["result"]["file_path"]
            file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
            async with s.get(file_url) as r2:
                if r2.status != 200:
                    raise HTTPException(status_code=404, detail="photo not found")
                return Response(content=await r2.read(), media_type="image/jpeg")
    except HTTPException:
        raise
    except Exception:
        log.exception("photo proxy failed")
        raise HTTPException(status_code=500, detail="photo proxy error")

# ========== TELEGRAM BOT ==========
BTN_SEND_POINT   = "📍 Send Current Volunteer Location"
BTN_LIVE_TRACK   = "🛰 Share Live Volunteer Location"
BTN_REPORT_FIRE  = "🔥 Report Fire"
BTN_VIEW_MAP     = "🗺 View Map"
BTN_CANCEL       = "🔕 Cancel"

KB_SEND_POINT = KeyboardButton(text=BTN_SEND_POINT, request_location=True)
KB_LIVE_TRACK = KeyboardButton(text=BTN_LIVE_TRACK)
KB_REPORT     = KeyboardButton(text=BTN_REPORT_FIRE)
KB_VIEW       = KeyboardButton(text=BTN_VIEW_MAP)
KB_CANCEL     = KeyboardButton(text=BTN_CANCEL)

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KB_SEND_POINT],[KB_LIVE_TRACK],[KB_REPORT],[KB_VIEW],[KB_CANCEL]],
    resize_keyboard=True, is_persistent=True
)

_user_mode: dict[int, tuple[str, int]] = {}
_last_loc: dict[int, tuple[float, float, int]] = {}

def cancel_mode(uid:int):
    _user_mode.pop(uid, None)

def guess_contact(msg: Message) -> str | None:
    u = msg.from_user
    return f"@{u.username}" if (u and u.username) else None

bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if TELEGRAM_TOKEN else None
dp  = Dispatcher() if TELEGRAM_TOKEN else None

@dp.message(F.text == "/start")
async def cmd_start(msg: Message):
    cancel_mode(msg.from_user.id)
    await msg.answer(
        "Available actions:\n"
        "1) 📍 Send current volunteer location\n"
        "2) 🛰 Share live volunteer location\n"
        "3) 🔥 Report fire\n"
        "4) 🗺 View map",
        reply_markup=MAIN_KB
    )
    kb = _map_button(msg.from_user.id)
    if kb:
        await msg.answer("Open the live map:", reply_markup=kb)

@dp.message(F.text == BTN_VIEW_MAP)
async def open_map(msg: Message):
    kb = _map_button(msg.from_user.id)
    if kb:
        await msg.answer("Open the live map:", reply_markup=kb)
    else:
        await msg.answer("Map is not available: check BASE_URL/MAP_URL.", reply_markup=MAIN_KB)

@dp.message(F.text == BTN_REPORT_FIRE)
async def report_fire(msg: Message):
    _user_mode[msg.from_user.id] = ("report_fire", int(time.time()))
    lat, lon = CENTER_LAT, CENTER_LON
    last = _last_loc.get(msg.from_user.id)
    if last and time.time()-last[2] < 1200:
        lat, lon = last[0], last[1]
    contact = guess_contact(msg)
    base = BASE_URL if _is_public_http(BASE_URL) else f"http://localhost:{PORT}"
    url = f"{base}/pick?mode=fire&lat={lat:.6f}&lon={lon:.6f}&z={CENTER_ZOOM}&contact={quote_plus(contact or '')}"
    kb = _map_button(msg.from_user.id)
    await msg.answer(
        "Open the picker, move the pin, copy coordinates and paste them here. "
        "Optionally attach a photo and a text description.\n" + url,
        reply_markup=MAIN_KB
    )
    if kb:
        await msg.answer("Open the live map:", reply_markup=kb)

@dp.message(F.text == BTN_LIVE_TRACK)
async def live_hint(msg: Message):
    await msg.answer(
        "To share your live location:\n"
        "📎 Attachment → Location → *Share Live Location*.\n"
        "I'll keep your marker moving on the map automatically.",
        reply_markup=MAIN_KB
    )

@dp.message(F.text == BTN_CANCEL)
async def cancel(msg: Message):
    cancel_mode(msg.from_user.id)
    await msg.answer("Mode cancelled.", reply_markup=MAIN_KB)

# ---------- regular & live locations ----------
@dp.message(F.location)
async def handle_location(msg: Message):
    loc = msg.location
    _last_loc[msg.from_user.id] = (loc.latitude, loc.longitude, int(time.time()))
    live_period = getattr(loc, "live_period", None)

    if live_period:  # start live
        now = int(time.time())
        eid = upsert_live_event(
            ts=now, user_id=msg.from_user.id,
            lat=loc.latitude, lon=loc.longitude,
            contact=guess_contact(msg),
            chat_id=msg.chat.id, msg_id=msg.message_id,
            live_until=now + int(live_period)
        )
        kb = _map_button(msg.from_user.id)
        await msg.answer(f"Live tracking started (id={eid}).", reply_markup=MAIN_KB)
        if kb:
            await msg.answer("Open the live map:", reply_markup=kb)
        return

    # non-live: treat as volunteer unless in fire mode
    mode = _user_mode.get(msg.from_user.id)
    is_fire = bool(mode and mode[0] == "report_fire" and int(time.time()) - mode[1] < 1200)
    typ = "fire" if is_fire else "volunteer"
    eid = save_event({
        "ts": int(time.time()), "type": typ,
        "lat": loc.latitude, "lon": loc.longitude,
        "user_id": msg.from_user.id, "group_id": None,
        "text": None, "photo_file_id": None,
        "status": "active", "contact": guess_contact(msg)
    })
    cancel_mode(msg.from_user.id)
    kb = _map_button(msg.from_user.id)
    await msg.answer(f"{'Fire' if typ=='fire' else 'Volunteer'} point added (id={eid}).", reply_markup=MAIN_KB)
    if kb:
        await msg.answer("Open the live map:", reply_markup=kb)

@dp.edited_message(F.location)
async def live_update(msg: Message):
    loc = msg.location
    if not loc:
        return
    update_live_coords(
        msg.chat.id, msg.message_id,
        ts=int(time.time()),
        lat=loc.latitude, lon=loc.longitude
    )

# <-- ВОТ ЭТА СТРОКА ИСПРАВЛЕНА: без Text, через regexp -->
@dp.message(F.text.regexp(r'(?i)^stop$'))
async def live_stop_cmd(msg: Message):
    stop_live(msg.chat.id, msg.message_id)
    await msg.answer("Live tracking stopped for the last message.", reply_markup=MAIN_KB)

# ---------- text coordinates ----------
def parse_coords_with_contact(s: str):
    if not s:
        return None, None, None, None
    txt = s.strip().replace(",", " ").replace(";", " ")
    parts = [p for p in txt.split() if p]
    if len(parts) < 2:
        return None, None, None, s
    contact = None
    tail_tokens = []
    for p in parts[2:]:
        if contact is None and (p.startswith("@") or p.startswith("+")):
            contact = p
        else:
            tail_tokens.append(p)
    try:
        lat = float(parts[0]); lon = float(parts[1])
        return lat, lon, contact, (" ".join(tail_tokens) if tail_tokens else None)
    except Exception:
        return None, None, None, s

@dp.message(F.text)
async def maybe_coords(msg: Message):
    text = (msg.text or "").strip()
    if text in (BTN_SEND_POINT, BTN_LIVE_TRACK, BTN_REPORT_FIRE, BTN_VIEW_MAP, BTN_CANCEL, "/start", "stop"):
        return
    raw = text
    is_fire_prefix = raw.lower().startswith("fire ")
    if is_fire_prefix:
        raw = raw[5:].strip()
    lat, lon, contact_txt, tail = parse_coords_with_contact(raw)
    if lat is None or lon is None:
        return

    mode = _user_mode.get(msg.from_user.id)
    is_fire = is_fire_prefix or bool(mode and mode[0] == "report_fire" and int(time.time()) - mode[1] < 1200)
    typ = "fire" if is_fire else "volunteer"
    eid = save_event({
        "ts": int(time.time()), "type": typ,
        "lat": lat, "lon": lon,
        "user_id": msg.from_user.id, "group_id": None,
        "text": tail, "photo_file_id": None,
        "status": "active", "contact": (contact_txt or guess_contact(msg))
    })
    cancel_mode(msg.from_user.id)
    kb = _map_button(msg.from_user.id)
    await msg.answer(f"{'Fire' if typ=='fire' else 'Volunteer'} point added (id={eid}).", reply_markup=MAIN_KB)
    if kb:
        await msg.answer("Open the live map:", reply_markup=kb)

# ---------- photos as fires ----------
@dp.message(F.photo)
async def got_photo(msg: Message):
    now = int(time.time())
    lat = lon = None
    last = _last_loc.get(msg.from_user.id)
    if last and now - last[2] < 1200:
        lat, lon = last[0], last[1]
    caption = (msg.caption or "").strip()
    if caption:
        parts = caption.replace(",", " ").split()
        if len(parts) >= 2:
            try:
                lat = float(parts[0]); lon = float(parts[1])
            except Exception:
                pass
    eid = save_event({
        "ts": now, "type": "fire",
        "lat": lat, "lon": lon,
        "user_id": msg.from_user.id, "group_id": None,
        "text": caption or None, "photo_file_id": None,
        "status": "active", "contact": guess_contact(msg)
    })
    try:
        file_id = msg.photo[-1].file_id
        add_photo_to_event(eid, file_id=file_id)
    except Exception:
        log.exception("add_photo_to_event failed")
    kb = _map_button(msg.from_user.id)
    await msg.answer(f"Photo received. Fire id={eid}.", reply_markup=MAIN_KB)
    if kb:
        await msg.answer("Open the live map:", reply_markup=kb)

# ---------- polling ----------
async def _run_bot():
    if not TELEGRAM_TOKEN:
        return
    await dp.start_polling(Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)))

@app.on_event("startup")
async def _maybe_start_bot():
    if TELEGRAM_TOKEN and not os.getenv("DISABLE_POLLING"):
        asyncio.create_task(_run_bot())
    else:
        log.warning("Bot polling is disabled.")
