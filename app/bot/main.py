# app/bot/main.py
import os
import io
import hmac
import time
import logging
from typing import Optional

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup, ContentType
)

from .storage import init_db, save_event, delete_event, get_geojson

# --------- конфигурация ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wildfire")

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

MAP_URL = os.getenv("MAP_URL") or os.getenv("BASE_URL", "http://localhost:8000")
CENTER_LAT = float(os.getenv("CENTER_LAT", "42.179"))
CENTER_LON = float(os.getenv("CENTER_LON", "18.942"))
CENTER_ZOOM = int(os.getenv("CENTER_ZOOM", "12"))
MAP_TILER_KEY = os.getenv("MAP_TILER_KEY", "")

SECRET_KEY = os.getenv("SECRET_KEY", "devsecret")

# --------- FastAPI ----------
app = FastAPI()
app.mount("/assets", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "webmap")), name="assets")

# Общая карта (вставляем параметры в HTML)
def _index_html() -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "webmap", "index.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__MAP_KEY__", MAP_TILER_KEY or "__MAP_KEY__")
    html = html.replace("__LAT__", str(CENTER_LAT))
    html = html.replace("__LON__", str(CENTER_LON))
    html = html.replace("__ZOOM__", str(CENTER_ZOOM))
    return html

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return HTMLResponse(_index_html())


@app.get("/geojson")
async def geojson():
    return JSONResponse(get_geojson())


# отдаём фото по file_id через Telegram
bot_for_download = Bot(TOKEN, parse_mode=ParseMode.HTML)

@app.get("/photo/{file_id}")
async def photo(file_id: str):
    try:
        file = await bot_for_download.get_file(file_id)
        buf = await bot_for_download.download_file(file.file_path)
        data = buf.read()
        return Response(content=data, media_type="image/jpeg")
    except Exception as e:
        raise HTTPException(status_code=404, detail="photo not found")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# --------- Telegram bot ----------
bot = Bot(TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

def sign_user(uid: int) -> str:
    return hmac.new(SECRET_KEY.encode(), str(uid).encode(), digestmod="sha256").hexdigest()

def _main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Send my location (instant)", request_location=True)],
            [KeyboardButton(text="📡 Share Live Location")],
            [KeyboardButton(text="🔥 Report Fire")],
            [KeyboardButton(text="🌍 Open Map")],
        ],
        resize_keyboard=True
    )

def _map_button(uid: Optional[int]=None) -> InlineKeyboardMarkup:
    url = MAP_URL
    if uid:
        url = f"{MAP_URL}/?uid={uid}&sig={sign_user(uid)}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🌍 Open live map", url=url)]]
    )
    return kb

@dp.message(CommandStart())
async def start_cmd(msg: Message):
    uid = msg.from_user.id
    await msg.answer(
        "Welcome! Use buttons below.\n\n"
        "• Instant volunteer location — send one-time location.\n"
        "• Share Live Location — start Telegram live location.\n"
        "• Report Fire — send fire point (coords/text/photo).\n"
        "• Open Map — see all points.",
        reply_markup=_main_kb()
    )
    await msg.answer("Live map:", reply_markup=_map_button(uid))

# 1) моментальная локация добровольца
@dp.message(F.location)
async def got_location(msg: Message):
    uid = msg.from_user.id
    contact = f"@{msg.from_user.username}" if msg.from_user.username else (msg.from_user.full_name or str(uid))
    lat = msg.location.latitude
    lon = msg.location.longitude
    eid = save_event(
        etype="volunteer",
        lat=lat, lon=lon,
        user_id=uid, contact=contact,
        text=None, photo_file_id=None
    )
    await msg.answer(f"Location saved (id={eid}).", reply_markup=_main_kb())
    await msg.answer("Open map:", reply_markup=_map_button(uid))

# 3) Report Fire — ожидаем координаты как текст, потом можно прислать фото/описание
FIRE_MODE: Dict[int, Dict[str, any]] = {}  # uid -> {lat,lon}

@dp.message(F.text == "🔥 Report Fire")
async def enter_fire_mode(msg: Message):
    uid = msg.from_user.id
    FIRE_MODE.pop(uid, None)
    await msg.answer(
        "Send coordinates as text: <code>lat,lon</code>.\n"
        "Then you may attach a photo and/or description.",
        reply_markup=_main_kb()
    )

@dp.message(F.text.regexp(r"^\s*[-+]?\d+(\.\d+)?,\s*[-+]?\d+(\.\d+)?\s*$"))
async def fire_coords_text(msg: Message):
    uid = msg.from_user.id
    contact = f"@{msg.from_user.username}" if msg.from_user.username else (msg.from_user.full_name or str(uid))
    txt = msg.text.strip()
    lat_s, lon_s = txt.split(",", 1)
    lat = float(lat_s)
    lon = float(lon_s)
    # создаём огонь сразу (без фото/описания), потом можно ещё раз прислать текст/фото — будет второй id
    eid = save_event(
        etype="fire",
        lat=lat, lon=lon,
        user_id=uid, contact=contact,
        text=None, photo_file_id=None
    )
    await msg.answer(f"Fire saved (id={eid}). You can send a photo or description in a new message.", reply_markup=_main_kb())
    await msg.answer("Open map:", reply_markup=_map_button(uid))

@dp.message(F.photo)
async def fire_with_photo(msg: Message):
    """Если пользователь до этого прислал координаты — добавит новую точку с фото.
       Либо можно в подписи к фото написать 'lat,lon' — тогда тоже добавим."""
    uid = msg.from_user.id
    contact = f"@{msg.from_user.username}" if msg.from_user.username else (msg.from_user.full_name or str(uid))
    caption = (msg.caption or "").strip()
    lat = lon = None
    if caption and "," in caption:
        try:
            lat_s, lon_s = caption.split(",", 1)
            lat = float(lat_s); lon = float(lon_s)
        except Exception:
            pass
    if lat is None or lon is None:
        await msg.answer("Please add coordinates in photo caption as <code>lat,lon</code>.")
        return
    file_id = msg.photo[-1].file_id
    eid = save_event(
        etype="fire",
        lat=lat, lon=lon,
        user_id=uid, contact=contact,
        text=None,
        photo_file_id=file_id
    )
    await msg.answer(f"Photo saved for fire (id={eid}).", reply_markup=_main_kb())
    await msg.answer("Open map:", reply_markup=_map_button(uid))

# 4) Open map
@dp.message(F.text == "🌍 Open Map")
async def open_map(msg: Message):
    uid = msg.from_user.id
    await msg.answer("Open map:", reply_markup=_map_button(uid))


# Удаление точки по inline-дейcтвию (через веб — /event/{id}?uid=&sig=)
@app.delete("/event/{event_id}")
async def delete_event_api(event_id: int, uid: Optional[int] = None, sig: Optional[str] = None):
    if uid is None or sig is None:
        raise HTTPException(status_code=400, detail="uid/sig required")
    good = (sign_user(int(uid)) == sig)
    if not good:
        raise HTTPException(status_code=403, detail="bad signature")
    ok = delete_event(event_id, int(uid))
    return {"ok": ok}


# --------- запуск бота (polling) ----------
@app.on_event("startup")
async def on_startup():
    init_db()
    log.info("DB ready")
    # запускаем polling в фоне
    import asyncio
    loop = asyncio.get_event_loop()
    loop.create_task(_run_polling())

async def _run_polling():
    log.info("Start polling")
    await dp.start_polling(bot)
