from __future__ import annotations
import os
import hmac
import time
import json
import asyncio
import logging
from hashlib import sha256
from urllib.parse import urlparse, quote_plus

import aiohttp
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

# ---------- загрузка env ----------
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CENTER_LAT = float(os.getenv("CENTER_LAT", "42.179"))
CENTER_LON = float(os.getenv("CENTER_LON", "18.942"))
CENTER_ZOOM = int(os.getenv("CENTER_ZOOM", "12"))
BASE_URL = os.getenv("BASE_URL", "").strip()
MAP_URL = os.getenv("MAP_URL", "").strip()  # публичный URL для кнопки
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me").encode()
PORT = int(os.getenv("PORT", "8000"))

WEBMAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "webmap"))

# ---------- логирование ----------
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("app")

# ---------- валидация URL для кнопки ----------
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

# если MAP_URL пустой/локальный, но есть пригодный BASE_URL — используем его
if not _is_public_http(MAP_URL) and _is_public_http(BASE_URL):
    MAP_URL = BASE_URL

# ---------- импорт хранилища ----------
from .storage import (
    init_db, migrate, save_event, fetch_geojson,
    add_photo_to_event, delete_event_by_owner
)

# ---------- FastAPI ----------
app = FastAPI(title="Wildfire MVP")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

# при старте БД
@app.on_event("startup")
async def _on_startup():
    init_db()
    migrate()
    log.info("DB ready")

# ---------- HMAC подпись для удаления ----------
def sign_uid(uid: int) -> str:
    return hmac.new(SECRET_KEY, str(uid).encode(), sha256).hexdigest()

def check_sig(uid: int, sig: str) -> bool:
    good = sign_uid(uid)
    return hmac.compare_digest(good, sig or "")

# ---------- утилиты ----------
def _read_template(name: str) -> str:
    with open(os.path.join(WEBMAP_DIR, name), "r", encoding="utf-8") as f:
        return f.read()

# персональная ссылка на карту (для кнопки «Удалить» в попапах)
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
        log.warning("MAP_URL/BASE_URL not public — skip map button")
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌍 Открыть карту", url=url)]
    ])

# ---------- страницы ----------
@app.get("/", response_class=HTMLResponse)
def index():
    html = _read_template("index.html")
    html = (html
            .replace("__LAT__", str(CENTER_LAT))
            .replace("__LON__", str(CENTER_LON))
            .replace("__ZOOM__", str(CENTER_ZOOM)))
    return HTMLResponse(html)

@app.get("/pick", response_class=HTMLResponse)
def pick(request: Request,
         lat: float = CENTER_LAT,
         lon: float = CENTER_LON,
         z: int = CENTER_ZOOM,
         mode: str = "vol",
         contact: str = ""):
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

# --- прокси для фото из Telegram ---
@app.get("/photo/{file_id}")
async def photo(file_id: str):
    if not TELEGRAM_TOKEN:
        raise HTTPException(status_code=404, detail="bot not configured")
    # отдельный Bot объект здесь не требуется: URL формируется напрямую
    try:
        # шаг 1: получаем путь файла через getFile
        async with aiohttp.ClientSession() as s:
            get_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            async with s.get(get_url) as r:
                data = await r.json()
                if not data.get("ok"):
                    raise HTTPException(status_code=404, detail="photo not found")
                file_path = data["result"]["file_path"]
            # шаг 2: скачиваем сам файл
            file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
            async with s.get(file_url) as r2:
                if r2.status != 200:
                    raise HTTPException(status_code=404, detail="photo not found")
                blob = await r2.read()
                return Response(content=blob, media_type="image/jpeg")
    except HTTPException:
        raise
    except Exception:
        logging.exception("photo proxy failed")
        raise HTTPException(status_code=500, detail="photo proxy error")

# ---------- Telegram bot ----------
VOL_BTN = "📍 Отправить локацию волонтёра"
FIRE_BTN = "🔥 Сообщить об очаге"
CANCEL_BTN = "🔕 Отменить режим"
PICK_BTN = "🧭 Выбрать точку на карте"

VOL_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=VOL_BTN)],
        [KeyboardButton(text=FIRE_BTN)],
        [KeyboardButton(text=PICK_BTN)],
        [KeyboardButton(text=CANCEL_BTN)],
    ],
    resize_keyboard=True, is_persistent=True
)

_user_mode: dict[int, tuple[str, int]] = {}   # user_id -> (mode, ts)
_last_loc: dict[int, tuple[float, float, int]] = {}

def cancel_mode(uid: int):
    _user_mode.pop(uid, None)

def guess_contact(msg: Message) -> str | None:
    u = msg.from_user
    if u and u.username:
        return f"@{u.username}"
    return None

def parse_coords_with_contact(s: str) -> tuple[float | None, float | None, str | None, str | None]:
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

bot: Bot | None = None
dp: Dispatcher | None = None

if TELEGRAM_TOKEN:
    bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
else:
    log.warning("TELEGRAM_TOKEN not set — bot polling will be disabled")

@dp.message(F.text == "/start")
async def start(msg: Message):
    cancel_mode(msg.from_user.id)
    kb = _map_button(msg.from_user.id)
    if kb:
        await msg.answer(
            "Привет! Отправьте локацию или координаты.\n"
            "Режимы: очаг 🔥 или волонтёр 📍.\nКарта доступна всем:",
            reply_markup=VOL_KB,
        )
        await msg.answer("Открыть карту:", reply_markup=kb)
    else:
        await msg.answer(
            "Привет! Карта пока недоступна (некорректный MAP_URL/BASE_URL).",
            reply_markup=VOL_KB
        )

@dp.message(F.text == "🌍 Открыть карту")
async def open_map_fallback(msg: Message):
    kb = _map_button(msg.from_user.id)
    if kb:
        await msg.answer("Открыть карту:", reply_markup=kb)
    else:
        await msg.answer("Карта недоступна: проверьте MAP_URL/BASE_URL.", reply_markup=VOL_KB)

def _pick_link(mode: str, lat: float, lon: float, contact: str | None) -> str:
    q = f"mode={mode}&lat={lat:.6f}&lon={lon:.6f}&z={CENTER_ZOOM}"
    if contact:
        q += f"&contact={quote_plus(contact)}"
    base = BASE_URL if _is_public_http(BASE_URL) else f"http://localhost:{PORT}"
    return f"{base}/pick?{q}"

@dp.message(F.text == VOL_BTN)
async def on_vol_btn(msg: Message):
    cancel_mode(msg.from_user.id)
    lat, lon = CENTER_LAT, CENTER_LON
    last = _last_loc.get(msg.from_user.id)
    if last and time.time() - last[2] < 1200:
        lat, lon = last[0], last[1]
    link = _pick_link("vol", lat, lon, guess_contact(msg))
    kb = _map_button(msg.from_user.id)
    await msg.answer(f"Выберите точку волонтёра:\n{link}", reply_markup=VOL_KB)
    if kb: await msg.answer("Открыть карту:", reply_markup=kb)

@dp.message(F.text == FIRE_BTN)
async def on_fire_btn(msg: Message):
    _user_mode[msg.from_user.id] = ("report_fire", int(time.time()))
    lat, lon = CENTER_LAT, CENTER_LON
    last = _last_loc.get(msg.from_user.id)
    if last and time.time() - last[2] < 1200:
        lat, lon = last[0], last[1]
    link = _pick_link("fire", lat, lon, guess_contact(msg))
    kb = _map_button(msg.from_user.id)
    await msg.answer(f"Выберите точку очага и пришлите координаты/фото.\n{link}", reply_markup=VOL_KB)
    if kb: await msg.answer("Открыть карту:", reply_markup=kb)

@dp.message(F.text == CANCEL_BTN)
async def on_cancel(msg: Message):
    cancel_mode(msg.from_user.id)
    await msg.answer("Режим сброшен.", reply_markup=VOL_KB)

@dp.message(F.text == "🧭 Выбрать точку на карте")
async def on_pick(msg: Message):
    lat, lon = CENTER_LAT, CENTER_LON
    last = _last_loc.get(msg.from_user.id)
    if last and time.time() - last[2] < 1200:
        lat, lon = last[0], last[1]
    link = _pick_link("vol", lat, lon, guess_contact(msg))
    await msg.answer(f"Страница выбора точки:\n{link}", reply_markup=VOL_KB)

# --- локации ---
@dp.message(F.location)
async def got_location(msg: Message):
    loc = msg.location
    _last_loc[msg.from_user.id] = (loc.latitude, loc.longitude, int(time.time()))
    mode = _user_mode.get(msg.from_user.id)
    is_fire = bool(mode and mode[0] == "report_fire" and int(time.time()) - mode[1] < 1200)
    typ = "fire" if is_fire else "volunteer"
    contact = guess_contact(msg)  # авто-контакт для обоих типов
    eid = save_event({
        "ts": int(time.time()), "type": typ,
        "lat": loc.latitude, "lon": loc.longitude,
        "user_id": msg.from_user.id, "group_id": None,
        "text": None, "photo_file_id": None,
        "status": "active", "contact": contact
    })
    cancel_mode(msg.from_user.id)  # после точки режим гасим
    kb = _map_button(msg.from_user.id)
    reply = f"✅ {('Очаг' if typ=='fire' else 'Волонтёр')} добавлен (id={eid})."
    await msg.answer(reply, reply_markup=VOL_KB)
    if kb: await msg.answer("Открыть карту:", reply_markup=kb)

# --- текстовые координаты ---
@dp.message(F.text)
async def maybe_coords(msg: Message):
    text = (msg.text or "").strip()
    if text in (VOL_BTN, FIRE_BTN, CANCEL_BTN, "🧭 Выбрать точку на карте", "/start"):
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
    contact = contact_txt or guess_contact(msg)

    eid = save_event({
        "ts": int(time.time()), "type": typ,
        "lat": lat, "lon": lon,
        "user_id": msg.from_user.id, "group_id": None,
        "text": tail, "photo_file_id": None,
        "status": "active", "contact": contact
    })
    cancel_mode(msg.from_user.id)
    kb = _map_button(msg.from_user.id)
    await msg.answer(f"✅ {('Очаг' if typ=='fire' else 'Волонтёр')} добавлен (id={eid}).", reply_markup=VOL_KB)
    if kb: await msg.answer("Открыть карту:", reply_markup=kb)

# --- фото (как очаг) ---
@dp.message(F.photo)
async def got_photo(msg: Message):
    now = int(time.time())
    lat = lon = None
    last = _last_loc.get(msg.from_user.id)
    if last and now - last[2] < 1200:
        lat, lon = last[0], last[1]
    caption = (msg.caption or "").strip()
    if caption:
        p_lat, p_lon, p_contact, _ = parse_coords_with_contact(caption)
        if p_lat is not None and p_lon is not None:
            lat, lon = p_lat, p_lon
    contact = guess_contact(msg)
    eid = save_event({
        "ts": now, "type": "fire",
        "lat": lat, "lon": lon,
        "user_id": msg.from_user.id, "group_id": None,
        "text": caption or None, "photo_file_id": None,
        "status": "active", "contact": contact
    })
    try:
        file_id = msg.photo[-1].file_id
        add_photo_to_event(eid, file_id=file_id)
    except Exception:
        log.exception("add_photo_to_event failed")
    kb = _map_button(msg.from_user.id)
    await msg.answer(f"📸 Фото получено. Очаг id={eid}.", reply_markup=VOL_KB)
    if kb: await msg.answer("Открыть карту:", reply_markup=kb)

# ---------- запуск бота-поллинга в фоновом таске ----------
async def _run_bot():
    assert TELEGRAM_TOKEN and dp
    log.info("Starting bot polling…")
    await dp.start_polling(Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)))

@app.on_event("startup")
async def _maybe_start_bot():
    if TELEGRAM_TOKEN and dp and not os.getenv("DISABLE_POLLING"):
        asyncio.create_task(_run_bot())
    else:
        log.warning("Bot polling is disabled (no token or DISABLE_POLLING=1).")
