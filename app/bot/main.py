import os
import re
import io
import csv
import hmac
import time
import asyncio
import logging
from typing import Optional, Tuple, List, Dict

import requests
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType, ChatType
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)

from .storage import (
    init_db, connect,
    save_event, delete_event,
    save_live_start, save_live_update, stop_live
)

# ---------------------- config ----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wildfire")

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

SECRET_KEY = os.getenv("SECRET_KEY", "dev")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

CENTER_LAT = float(os.getenv("CENTER_LAT", "42.179"))
CENTER_LON = float(os.getenv("CENTER_LON", "18.942"))
CENTER_ZOOM = int(os.getenv("CENTER_ZOOM", "12"))

MAP_TILER_KEY = os.getenv("MAP_TILER_KEY", "").strip()

NASA_API_KEY = os.getenv("NASA_API_KEY", "").strip()
FIRMS_BBOX = os.getenv("FIRMS_BBOX", "18.3,41.8,20.4,43.6")
FIRMS_DAYS = int(os.getenv("FIRMS_DAYS", "1"))

# ---------------------- ASGI app ----------------------
app = FastAPI()
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "webmap")
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

def _render_index() -> str:
    path = os.path.join(ASSETS_DIR, "index.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    return (html
            .replace("const DEFAULT_CENTER = [42.179, 18.942];", f"const DEFAULT_CENTER = [{CENTER_LAT}, {CENTER_LON}];")
            .replace("const DEFAULT_ZOOM   = 12;", f"const DEFAULT_ZOOM   = {CENTER_ZOOM};")
            .replace('const MAP_TILER_KEY  = "SC5bBhZz9sPQyDQTyEez";', f'const MAP_TILER_KEY  = "{MAP_TILER_KEY or "disable"}";')
            )

def _render_pick(uid: Optional[int], sig: Optional[str], init_lat: Optional[float], init_lon: Optional[float]) -> str:
    """
    Вставляем центр/зум/ключ и ссылку "Open live map" с uid/sig.
    Начальная позиция булавки — либо lat/lon из query, либо дефолтный центр.
    """
    path = os.path.join(ASSETS_DIR, "pick.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    openmap_url = BASE_URL
    if uid and sig:
        openmap_url = f"{BASE_URL}/?uid={uid}&sig={sig}"

    lat = init_lat if init_lat is not None else CENTER_LAT
    lon = init_lon if init_lon is not None else CENTER_LON

    return (html
            .replace("__INIT_LAT__", str(lat))
            .replace("__INIT_LON__", str(lon))
            .replace("__CENTER_LAT__", str(CENTER_LAT))
            .replace("__CENTER_LON__", str(CENTER_LON))
            .replace("__CENTER_ZOOM__", str(CENTER_ZOOM))
            .replace("__MAP_TILER_KEY__", MAP_TILER_KEY or "disable")
            .replace("__OPENMAP_URL__", openmap_url)
            )

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_render_index())

@app.get("/pick", response_class=HTMLResponse)
async def pick(uid: Optional[int] = None, sig: Optional[str] = None,
               lat: Optional[float] = None, lon: Optional[float] = None):
    # /pick доступен всем; uid/sig нужны только для удобной ссылки "Open live map"
    return HTMLResponse(_render_pick(uid, sig, lat, lon))

@app.get("/healthz")
async def healthz():
    return {"ok": True, "time": int(time.time())}

# ---------------------- GeoJSON API ----------------------
def _feat_event(row) -> Dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [float(row["lon"]), float(row["lat"])]},
        "properties": {
            "id": int(row["id"]),
            "type": str(row["type"]),
            "user_id": int(row["user_id"]),
            "contact": row["username"],
            "text": row["text"],
            "photo_file_id": row["photo_file_id"],
            "ts": int(row["ts"]),
        },
    }

def _feat_live(row) -> Dict:
    fid = -int(row["user_id"])  # стабильный отрицательный id
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [float(row["lon"]), float(row["lat"])]},
        "properties": {
            "id": fid,
            "type": "volunteer_live",
            "user_id": int(row["user_id"]),
            "contact": row["username"],
            "text": None,
            "photo_file_id": None,
            "ts": int(row["ts"]),
            "live_until": int(row["live_until"]),
        },
    }

@app.get("/geojson")
async def geojson():
    feats: List[Dict] = []
    with connect() as con:
        cur = con.cursor()
        cur.execute("SELECT id,type,user_id,username,lat,lon,ts,text,photo_file_id FROM events ORDER BY ts DESC;")
        feats.extend(_feat_event(r) for r in cur.fetchall())
        cur.execute("SELECT user_id,username,lat,lon,ts,live_until FROM live WHERE live_until >= ?", (int(time.time()),))
        feats.extend(_feat_live(r) for r in cur.fetchall())
    return {"type": "FeatureCollection", "features": feats}

# ---------------------- deletion APIs ----------------------
def _sign(uid: int) -> str:
    return hmac.new(SECRET_KEY.encode(), f"{uid}:{SECRET_KEY}".encode(), "sha256").hexdigest()

def _check(uid: int, sig: str) -> bool:
    try:
        return hmac.compare_digest(sig, _sign(uid))
    except Exception:
        return False

@app.delete("/event/{event_id}")
async def api_delete_event(event_id: int, uid: Optional[int] = None, sig: Optional[str] = None):
    if uid is None or sig is None or not _check(int(uid), sig):
        raise HTTPException(status_code=403, detail="bad signature")
    ok = delete_event(event_id, int(uid))
    return {"ok": ok}

@app.delete("/live/{user_id}")
async def api_delete_live(user_id: int, uid: Optional[int] = None, sig: Optional[str] = None):
    if uid is None or sig is None or int(uid) != int(user_id) or not _check(int(uid), sig):
        raise HTTPException(status_code=403, detail="bad signature")
    stop_live(int(user_id))
    return {"ok": True}

# ---------------------- photos (Telegram proxy) ----------------------
def make_bot(token: str) -> Bot:
    try:
        return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    except Exception:
        return Bot(token, parse_mode=ParseMode.HTML)

bot_files = make_bot(TOKEN)

@app.get("/photo/{file_id}")
async def photo(file_id: str):
    try:
        file = await bot_files.get_file(file_id)
        buf = await bot_files.download_file(file.file_path)
        return Response(content=buf.read(), media_type="image/jpeg")
    except Exception:
        raise HTTPException(status_code=404, detail="photo not found")

# ---------------------- NASA FIRMS ----------------------
def _firms_urls() -> List[str]:
    if not NASA_API_KEY:
        return []
    bbox = FIRMS_BBOX
    days = str(max(1, min(FIRMS_DAYS, 7)))
    return [
        f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{NASA_API_KEY}/VIIRS_SNPP_NRT/{bbox}/{days}",
        f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{NASA_API_KEY}/VIIRS_NOAA20_NRT/{bbox}/{days}",
        f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{NASA_API_KEY}/MODIS_NRT/{bbox}/{days}",
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
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props})
    return feats

@app.get("/hotspots")
async def hotspots():
    urls = _firms_urls()
    if not urls:
        return {"type": "FeatureCollection", "features": []}
    features: List[Dict] = []
    for u in urls:
        try:
            r = requests.get(u, timeout=15)
            if r.status_code == 200 and r.text.strip():
                features.extend(_csv_to_features(r.text))
        except Exception as e:
            log.warning("FIRMS fetch failed %s: %s", u, e)
    return {"type": "FeatureCollection", "features": features}

@app.get("/hotspots/debug")
async def hotspots_debug():
    return {"urls": _firms_urls(), "has_key": bool(NASA_API_KEY), "bbox": FIRMS_BBOX, "days": FIRMS_DAYS}

# ---------------------- bot (aiogram) ----------------------
bot = make_bot(TOKEN)
dp = Dispatcher()

def _kb_main() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="📍 Send my location", request_location=True)],
        [KeyboardButton(text="🟢 Share live location")],
        [KeyboardButton(text="🔥 Report fire")],
        [KeyboardButton(text="🌍 Open live map")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def _map_btn(uid: int) -> InlineKeyboardMarkup:
    sig = _sign(uid)
    url = f"{BASE_URL}/?uid={uid}&sig={sig}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌍 Open live map", url=url)]
    ])

def _pick_btn(uid: int) -> InlineKeyboardMarkup:
    sig = _sign(uid)
    url = f"{BASE_URL}/pick?uid={uid}&sig={sig}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Open picker", url=url)]
    ])

def _user_contact(m: Message) -> str:
    if m.from_user.username:
        return f"@{m.from_user.username}"
    return m.from_user.full_name or str(m.from_user.id)

@dp.message(CommandStart(), F.chat.type.in_({ChatType.PRIVATE}))
async def on_start_cmd(msg: Message):
    await msg.answer("Hi! Choose an action:", reply_markup=_kb_main())
    await msg.answer("Open the live map:", reply_markup=_map_btn(msg.from_user.id))

# ---- единый обработчик любой локации ----
@dp.message(F.content_type == ContentType.LOCATION)
async def on_any_location(msg: Message):
    if not msg.location:
        return
    lp = msg.location.live_period
    lat, lon = msg.location.latitude, msg.location.longitude
    uid = msg.from_user.id
    now = int(time.time())

    if isinstance(lp, int) and lp > 0:
        save_live_start(
            uid=uid,
            username=_user_contact(msg),
            lat=lat, lon=lon,
            ts=now,
            live_until=now + lp
        )
        await msg.answer("Live location started 🟢", reply_markup=_kb_main())
    else:
        save_event(
            type="volunteer",
            user_id=uid,
            username=_user_contact(msg),
            lat=lat, lon=lon,
            ts=now, text=None, photo_file_id=None
        )
        await msg.answer("Location saved ✅", reply_markup=_kb_main())

    await msg.answer("Open the live map:", reply_markup=_map_btn(uid))

@dp.edited_message(F.content_type == ContentType.LOCATION)
async def live_update(msg: Message):
    if not msg.location:
        return
    save_live_update(
        uid=msg.from_user.id,
        lat=msg.location.latitude,
        lon=msg.location.longitude,
        ts=int(time.time())
    )

# 3) report fire -> picker + coords/photo/text
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

class AddFire(StatesGroup):
    awaiting_coords = State()
    awaiting_optional = State()

@dp.message(F.text == "🔥 Report fire")
async def fire_begin(msg: Message, state: FSMContext):
    await state.set_state(AddFire.awaiting_coords)
    await msg.answer("Open picker and paste coordinates here, or send a location.", reply_markup=_pick_btn(msg.from_user.id))

def _parse_coords(text: str) -> Optional[Tuple[float, float]]:
    m = re.search(r"(-?\d+(?:\.\d+)?)[,;\s]+(-?\d+(?:\.\d+)?)", text or "")
    if not m: return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon

@dp.message(AddFire.awaiting_coords, F.content_type == ContentType.LOCATION)
async def fire_coords_from_loc(msg: Message, state: FSMContext):
    await state.update_data(coords=(msg.location.latitude, msg.location.longitude))
    await state.set_state(AddFire.awaiting_optional)
    await msg.answer("Got coordinates. Send photo and/or text (optional) or 'OK' to finish.", reply_markup=_kb_main())

@dp.message(AddFire.awaiting_coords, F.text)
async def fire_coords_from_text(msg: Message, state: FSMContext):
    pts = _parse_coords(msg.text)
    if not pts:
        return await msg.answer("Send coordinates like <code>42.179, 18.942</code> or share a location.")
    await state.update_data(coords=pts)
    await state.set_state(AddFire.awaiting_optional)
    await msg.answer("Got coordinates. Send photo and/or text (optional) or 'OK' to finish.", reply_markup=_kb_main())

@dp.message(AddFire.awaiting_optional, F.photo | F.text)
async def fire_finish(msg: Message, state: FSMContext):
    data = await state.get_data()
    coords = data.get("coords")
    if not coords:
        await state.clear()
        return await msg.answer("Cancelled.", reply_markup=_kb_main())

    lat, lon = coords
    text = None
    photo_id = None
    if msg.photo:
        photo_id = msg.photo[-1].file_id
        if msg.caption:
            text = msg.caption
    elif msg.text and msg.text.lower() != "ok":
        text = msg.text

    save_event(
        type="fire",
        user_id=msg.from_user.id,
        username=_user_contact(msg),
        lat=lat, lon=lon, ts=int(time.time()),
        text=text, photo_file_id=photo_id
    )
    await state.clear()
    await msg.answer("Fire point added 🔥✅", reply_markup=_kb_main())
    await msg.answer("Open the live map:", reply_markup=_map_btn(msg.from_user.id))

@dp.message(F.text == "🌍 Open live map")
async def open_map(msg: Message):
    await msg.answer("Open the live map:", reply_markup=_map_btn(msg.from_user.id))

# ---------------------- background: start polling ----------------------
@app.on_event("startup")
async def _startup():
    init_db()
    log.info("DB ready")
    loop = asyncio.get_event_loop()
    log.info("Start polling")
    loop.create_task(dp.start_polling(bot))
