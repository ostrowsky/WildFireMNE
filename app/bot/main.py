import os
import io
import csv
import hmac
import time
import asyncio
import logging
from typing import Optional, List, Dict

import requests
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
)

from .storage import (
    init_db, add_event, delete_event, get_geojson,
    upsert_live_start, update_live_position, remove_live, purge_expired_live
)

# --------- Config ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wildfire")

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

SECRET_KEY = os.getenv("SECRET_KEY", "devsecret")

CENTER_LAT = float(os.getenv("CENTER_LAT", "42.179"))
CENTER_LON = float(os.getenv("CENTER_LON", "18.942"))
CENTER_ZOOM = int(os.getenv("CENTER_ZOOM", "12"))
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
MAP_TILER_KEY = os.getenv("MAP_TILER_KEY", "")

NASA_API_KEY = os.getenv("NASA_API_KEY", "").strip()
FIRMS_BBOX = os.getenv("FIRMS_BBOX", "18.3,41.8,20.4,43.6")
FIRMS_DAYS = int(os.getenv("FIRMS_DAYS", "1"))

# --------- FastAPI ----------
app = FastAPI()
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "webmap")
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

def _render_index() -> str:
    path = os.path.join(ASSETS_DIR, "index.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    return (html
            .replace("__LAT__", str(CENTER_LAT))
            .replace("__LON__", str(CENTER_LON))
            .replace("__ZOOM__", str(CENTER_ZOOM))
            .replace("__MAP_KEY__", MAP_TILER_KEY or "__MAP_KEY__")
            )

@app.get("/", response_class=HTMLResponse)
async def index(_: Request):
    return HTMLResponse(_render_index())

@app.get("/geojson")
async def geojson():
    return JSONResponse(get_geojson())

# serve Telegram photo by file_id
bot_dl = Bot(TOKEN, parse_mode=ParseMode.HTML)

@app.get("/photo/{file_id}")
async def photo(file_id: str):
    try:
        file = await bot_dl.get_file(file_id)
        buf = await bot_dl.download_file(file.file_path)
        return Response(content=buf.read(), media_type="image/jpeg")
    except Exception:
        raise HTTPException(status_code=404, detail="photo not found")

def sign_user(uid: int) -> str:
    return hmac.new(SECRET_KEY.encode(), str(uid).encode(), digestmod="sha256").hexdigest()

@app.delete("/event/{event_id}")
async def api_delete_event(event_id: int, uid: Optional[int] = None, sig: Optional[str] = None):
    if uid is None or sig is None:
        raise HTTPException(status_code=400, detail="uid/sig required")
    if sign_user(int(uid)) != sig:
        raise HTTPException(status_code=403, detail="bad signature")
    ok = delete_event(event_id, int(uid))
    return {"ok": ok}

@app.delete("/live/{user_id}")
async def api_delete_live(user_id: int, uid: Optional[int] = None, sig: Optional[str] = None):
    if uid is None or sig is None:
        raise HTTPException(status_code=400, detail="uid/sig required")
    if int(uid) != int(user_id) or sign_user(int(uid)) != sig:
        raise HTTPException(status_code=403, detail="bad signature")
    ok = remove_live(int(user_id))
    return {"ok": ok}

# ---- NASA FIRMS (CSV to GeoJSON) ----
def _firms_sources() -> List[str]:
    k = NASA_API_KEY
    bbox = FIRMS_BBOX
    days = str(max(1, min(FIRMS_DAYS, 7)))
    return [
        f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{k}/VIIRS_SNPP_NRT/{bbox}/{days}",
        f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{k}/VIIRS_NOAA20_NRT/{bbox}/{days}",
        f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{k}/MODIS_NRT/{bbox}/{days}",
    ]

def _csv_to_features(text: str) -> List[Dict]:
    feats: List[Dict] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        try:
            lat = float(row.get("latitude") or row.get("LATITUDE") or row.get("lat") or "")
            lon = float(row.get("longitude") or row.get("LONGITUDE") or row.get("lon") or "")
        except Exception:
            continue
        props = {
            "acq_date": row.get("acq_date") or row.get("ACQ_DATE"),
            "acq_time": row.get("acq_time") or row.get("ACQ_TIME"),
            "confidence": row.get("confidence") or row.get("CONFIDENCE"),
            "src": row.get("instrument") or row.get("satellite") or "",
        }
        feats.append({"type":"Feature","geometry":{"type":"Point","coordinates":[lon,lat]},"properties":props})
    return feats

@app.get("/hotspots")
async def hotspots():
    if not NASA_API_KEY:
        return {"type":"FeatureCollection","features":[]}
    features: List[Dict] = []
    for url in _firms_sources():
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200 and r.text.strip():
                features.extend(_csv_to_features(r.text))
        except Exception as e:
            log.warning("FIRMS failed for %s: %s", url, e)
    return {"type":"FeatureCollection","features":features}

@app.get("/hotspots/debug")
async def hotspots_debug():
    return {"parsed_urls": _firms_sources(), "bbox": FIRMS_BBOX, "days": FIRMS_DAYS, "has_key": bool(NASA_API_KEY)}

@app.get("/healthz")
async def healthz():
    return {"ok": True, "time": int(time.time())}

# --------- Telegram bot (aiogram v3) ----------
bot = Bot(TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

def _kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Send my location (instant)", request_location=True)],
            [KeyboardButton(text="📡 Share Live Location")],
            [KeyboardButton(text="🔥 Report Fire")],
            [KeyboardButton(text="🌍 Open Map")],
        ],
        resize_keyboard=True
    )

def _map_button(uid: Optional[int] = None) -> InlineKeyboardMarkup:
    url = BASE_URL
    if uid:
        sig = sign_user(uid)
        url = f"{BASE_URL}/?uid={uid}&sig={sig}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌍 Open live map", url=url)],
        [InlineKeyboardButton(text="🗺 Open without tiles", url=(url + ("&" if "?" in url else "?") + "nobase=1"))]
    ])

@dp.message(CommandStart())
async def on_start(msg: Message):
    uid = msg.from_user.id
    await msg.answer(
        "Welcome! Use the buttons below:\n\n"
        "• 📍 Send my location (instant)\n"
        "• 📡 Share Live Location (Telegram live)\n"
        "• 🔥 Report Fire (coords/photo/text)\n"
        "• 🌍 Open Map (live web map)",
        reply_markup=_kb_main()
    )
    await msg.answer("Open map:", reply_markup=_map_button(uid))

# --------- Instant volunteer location (single) ----------
# Универсальные фильтры для aiogram 3.x (без .as_(bool)):
@dp.message(F.location & ((F.location.live_period == None) | (F.location.live_period <= 0)))
async def on_instant_location(msg: Message):
    uid = msg.from_user.id
    contact = f"@{msg.from_user.username}" if msg.from_user.username else (msg.from_user.full_name or str(uid))
    lat = msg.location.latitude
    lon = msg.location.longitude
    eid = add_event(etype="volunteer", lat=lat, lon=lon, user_id=uid, contact=contact)
    await msg.answer(f"Location saved (id={eid}).", reply_markup=_kb_main())
    await msg.answer("Open map:", reply_markup=_map_button(uid))

# --------- Live Location: start (has live_period > 0) ----------
@dp.message(F.location & (F.location.live_period > 0))
async def on_live_start(msg: Message):
    uid = msg.from_user.id
    lp = int(msg.location.live_period or 0)
    contact = f"@{msg.from_user.username}" if msg.from_user.username else (msg.from_user.full_name or str(uid))
    lat = msg.location.latitude
    lon = msg.location.longitude
    live_until = int(msg.date.timestamp()) + max(0, lp)
    upsert_live_start(uid, contact, lat, lon, live_until)
    mins = max(1, lp // 60) if lp else 1
    await msg.answer(f"Live location started for ~{mins} min. I'll track updates until it ends.", reply_markup=_kb_main())
    await msg.answer("Open map:", reply_markup=_map_button(uid))

# --------- Live Location: updates (edited_message carries location) ----------
@dp.edited_message(F.location)
async def on_live_update(msg: Message):
    uid = msg.from_user.id
    lat = msg.location.latitude
    lon = msg.location.longitude
    update_live_position(uid, lat, lon)
    # silent ack

# --------- Fire report flow ----------
@dp.message(F.text == "🔥 Report Fire")
async def report_fire(msg: Message):
    await msg.answer(
        "Send fire coordinates as text: <code>lat,lon</code>.\n"
        "Optionally attach a photo and/or description after that.",
        reply_markup=_kb_main()
    )

@dp.message(F.text.regexp(r"^\s*[-+]?\d+(\.\d+)?,\s*[-+]?\d+(\.\d+)?\s*$"))
async def fire_text_coords(msg: Message):
    uid = msg.from_user.id
    contact = f"@{msg.from_user.username}" if msg.from_user.username else (msg.from_user.full_name or str(uid))
    lat_s, lon_s = msg.text.strip().split(",", 1)
    lat = float(lat_s); lon = float(lon_s)
    eid = add_event(etype="fire", lat=lat, lon=lon, user_id=uid, contact=contact)
    await msg.answer(f"Fire saved (id={eid}). You can now send a photo or description.", reply_markup=_kb_main())
    await msg.answer("Open map:", reply_markup=_map_button(uid))

@dp.message(F.photo)
async def fire_with_photo(msg: Message):
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
        return await msg.answer("Please add coordinates in photo caption as <code>lat,lon</code>.")
    file_id = msg.photo[-1].file_id
    eid = add_event(etype="fire", lat=lat, lon=lon, user_id=uid, contact=contact, photo_file_id=file_id)
    await msg.answer(f"Photo saved (fire id={eid}).", reply_markup=_kb_main())
    await msg.answer("Open map:", reply_markup=_map_button(uid))

@dp.message(F.text == "🌍 Open Map")
async def open_map_cmd(msg: Message):
    await msg.answer("Open map:", reply_markup=_map_button(msg.from_user.id))

# --------- Background tasks ----------
async def _live_gc_loop():
    while True:
        try:
            removed = purge_expired_live()
            if removed:
                log.info("Live GC: removed %s expired entries", removed)
        except Exception as e:
            log.warning("Live GC error: %s", e)
        await asyncio.sleep(60)

@app.on_event("startup")
async def on_startup():
    init_db()
    log.info("DB ready")
    loop = asyncio.get_event_loop()
    loop.create_task(_run_bot())
    loop.create_task(_live_gc_loop())

async def _run_bot():
    log.info("Start polling")
    await dp.start_polling(bot)
