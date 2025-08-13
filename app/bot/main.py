import asyncio, os, time, re, hmac, hashlib
from typing import Dict, Tuple, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

import requests

from .storage import init_db, save_event, fetch_geojson, get_event, add_photo, get_photo, get_photos, delete_event_for_user

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CENTER_LAT = float(os.getenv("CENTER_LAT", "42.179"))
CENTER_LON = float(os.getenv("CENTER_LON", "18.942"))
CENTER_ZOOM = int(os.getenv("CENTER_ZOOM", "12"))
BASE_URL = (os.getenv("BASE_URL") or "http://localhost:8000").rstrip("/")
MAP_URL = (os.getenv("MAP_URL") or BASE_URL).rstrip("/")
SECRET_KEY = (os.getenv("SECRET_KEY") or "dev-secret-change-me").encode()

app = FastAPI()
app.mount("/webmap", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "webmap")), name="webmap")

def sign(uid: int) -> str:
    return hmac.new(SECRET_KEY, str(uid).encode(), hashlib.sha256).hexdigest()[:16]

@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(os.path.dirname(__file__), "..", "webmap", "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
        html = (html.replace("__CENTER_LAT__", str(CENTER_LAT))
                    .replace("__CENTER_LON__", str(CENTER_LON))
                    .replace("__CENTER_ZOOM__", str(CENTER_ZOOM)))
        return HTMLResponse(content=html)

@app.get("/data.geojson")
def data_geojson():
    return JSONResponse(fetch_geojson())

@app.get("/pick", response_class=HTMLResponse)
def pick(request: Request,
         lat: float = CENTER_LAT, lon: float = CENTER_LON, z: int = CENTER_ZOOM,
         mode: str = "vol", contact: str = ""):
    with open(os.path.join(os.path.dirname(__file__), "..", "webmap", "pick.html"), "r", encoding="utf-8") as f:
        html = f.read()
        html = (html.replace("__LAT__", str(lat))
                    .replace("__LON__", str(lon))
                    .replace("__ZOOM__", str(z))
                    .replace("__MODE__", "fire" if mode.lower()=="fire" else "vol")
                    .replace("__CONTACT__", contact))
        return HTMLResponse(content=html)

@app.get("/photos/{event_id}")
def photos_list(event_id: int):
    rows = get_photos(event_id)
    return {'event_id': event_id, 'photos': [{'photo_id': r[0], 'ts': r[2], 'url': f"/photo/{r[0]}"} for r in rows]}

@app.get("/photo/{photo_id}")
def photo(photo_id: int):
    row = get_photo(photo_id)
    if not row:
        raise HTTPException(status_code=404, detail="photo not found")
    _, _, _, file_id = row
    photos_dir = os.path.join(os.path.dirname(__file__), "..", "photos")
    os.makedirs(photos_dir, exist_ok=True)
    cache_path = os.path.join(photos_dir, f"photo_{photo_id}.jpg")
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return Response(f.read(), media_type="image/jpeg")
    r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile", params={"file_id": file_id}, timeout=30).json()
    if not r.get("ok"):
        raise HTTPException(status_code=502, detail="telegram getFile failed")
    fp = r["result"]["file_path"]
    im = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{fp}", timeout=60)
    if im.status_code != 200:
        raise HTTPException(status_code=502, detail="telegram file download failed")
    with open(cache_path, "wb") as f:
        f.write(im.content)
    return Response(im.content, media_type="image/jpeg")

@app.delete("/event/{event_id}")
def delete_event(event_id: int, uid: int, sig: str):
    if sig != sign(uid):
        raise HTTPException(status_code=403, detail="bad signature")
    ok = delete_event_for_user(event_id, uid)
    if not ok:
        raise HTTPException(status_code=403, detail="not owner or not found")
    return {"ok": True}

@app.get("/healthz")
def healthz():
    return {"ok": True}

# --- Telegram bot (polling) ---
bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None

def _tg_contact(msg: Message) -> Optional[str]:
    try:
        u = msg.from_user
        if u and u.username:
            return f"@{u.username}"
    except Exception:
        pass
    return None

VOL_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📍 Отправить локацию волонтёра", request_location=True)],
        [KeyboardButton(text="🔥 Сообщить об очаге"), KeyboardButton(text="🔕 Отменить режим")],
        [KeyboardButton(text="🧭 Выбрать точку на карте")],
        [KeyboardButton(text="🗺 Открыть карту")]
    ],
    resize_keyboard=True,
    is_persistent=True,
    one_time_keyboard=False
)

_last_location: Dict[int, Tuple[float, float, int]] = {}
_user_mode: Dict[int, Tuple[str, int]] = {}
_last_event_by_user: Dict[int, Tuple[int, int]] = {}
_last_event_by_media_group: Dict[str, int] = {}

COORD_RE = re.compile(r'^\s*(?:fire|vol)?\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)(?:\s+(.*))?$', re.I)

def _map_button():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗺 Открыть карту", url=MAP_URL)]])

def _pick_link(mode: str, uid: int, contact: Optional[str]) -> str:
    lat, lon = CENTER_LAT, CENTER_LON
    last = _last_location.get(uid)
    if last and int(time.time()) - last[2] < 1200:
        lat, lon = last[0], last[1]
    from urllib.parse import quote_plus
    q = f"mode={mode}&lat={lat:.6f}&lon={lon:.6f}"
    if contact: q += f"&contact={quote_plus(contact)}"
    return f"{BASE_URL}/pick?{q}"

async def on_startup_polling():
    global bot, dp
    bot = Bot(TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None
    dp = Dispatcher()

    @dp.message(F.text.in_({"/start", "start"}))
    async def cmd_start(msg: Message):
        await msg.answer(f"Живая карта: {MAP_URL}", reply_markup=VOL_KB)
        await msg.answer("Карта для выбора точки:", reply_markup=VOL_KB)
        await msg.answer(_pick_link("vol", msg.from_user.id, _tg_contact(msg)), reply_markup=VOL_KB)

    @dp.message(F.text == "🗺 Открыть карту")
    async def open_map(msg: Message):
        await msg.answer("Открыть карту:", reply_markup=_map_button())

    @dp.message(F.text == "📍 Отправить локацию волонтёра")
    async def btn_vol(msg: Message):
        _user_mode.pop(msg.from_user.id, None)
        await msg.answer("Выберите точку и вставьте координаты:", reply_markup=VOL_KB)
        await msg.answer(_pick_link("vol", msg.from_user.id, _tg_contact(msg)), reply_markup=_map_button())

    @dp.message(F.text == "🔥 Сообщить об очаге")
    async def btn_fire(msg: Message):
        _user_mode[msg.from_user.id] = ("report_fire", int(time.time()))
        await msg.answer("Выберите точку очага и вставьте координаты:", reply_markup=VOL_KB)
        await msg.answer(_pick_link("fire", msg.from_user.id, _tg_contact(msg)), reply_markup=_map_button())

    @dp.message(F.text == "🔕 Отменить режим")
    async def btn_cancel(msg: Message):
        _user_mode.pop(msg.from_user.id, None)
        await msg.answer("Режим сброшен.", reply_markup=VOL_KB)

    @dp.message(F.text == "🧭 Выбрать точку на карте")
    async def btn_pick(msg: Message):
        _user_mode.pop(msg.from_user.id, None)
        await msg.answer(_pick_link("vol", msg.from_user.id, _tg_contact(msg)), reply_markup=VOL_KB)

    @dp.message(F.location)
    async def got_location(msg: Message):
        lat = msg.location.latitude; lon = msg.location.longitude
        now = int(time.time())
        _last_location[msg.from_user.id] = (lat, lon, now)
        mode = _user_mode.get(msg.from_user.id)
        make_fire = bool(mode and mode[0] == "report_fire" and now - mode[1] < 1200)
        typ = "fire" if make_fire else "volunteer"
        contact = _tg_contact(msg)  # auto contact for both
        event_id = save_event({'ts': now,'type': typ,'lat': lat,'lon': lon,
                               'user_id': msg.from_user.id,'group_id': None,
                               'text': None,'photo_file_id': None,'status': 'active',
                               'contact': contact})
        if make_fire: _user_mode.pop(msg.from_user.id, None)
        link = f"{BASE_URL}/?focus={event_id}&uid={msg.from_user.id}&sig={sign(msg.from_user.id)}"
        gmaps = f"https://www.google.com/maps?q={lat},{lon}"
        await msg.answer(("Очаг зарегистрирован 🔥" if typ=='fire' else "Позиция волонтёра зафиксирована ✅") + f"\nЖивая карта: {MAP_URL}\nФокус: {link}\nGoogle Maps: {gmaps}", reply_markup=_map_button())

    @dp.message(F.photo)
    async def got_photo(msg: Message):
        now = int(time.time())
        caption = (msg.caption or "").strip()
        lat = lon = None
        # try parse coords in caption
        m = COORD_RE.match(caption) if caption else None
        tail = None
        if m:
            lat, lon, tail = float(m.group(1)), float(m.group(2)), (m.group(3) or "").strip()
            caption = tail or ""
        event_id = None
        if msg.media_group_id:
            event_id = _last_event_by_media_group.get(msg.media_group_id)
        if not event_id:
            le = _last_event_by_user.get(msg.from_user.id)
            if le and now - le[1] < 1200: event_id = le[0]
        if not event_id:
            if (lat is None or lon is None):
                last = _last_location.get(msg.from_user.id)
                if last and now - last[2] < 1200: lat, lon = last[0], last[1]
            contact = _tg_contact(msg)
            event_id = save_event({'ts': now,'type': 'fire','lat': lat,'lon': lon,'user_id': msg.from_user.id,
                                   'group_id': None,'text': caption or None,'photo_file_id': None,'status': 'active',
                                   'contact': contact})
            _last_event_by_user[msg.from_user.id] = (event_id, now)
        add_photo(event_id, now, msg.photo[-1].file_id)
        if msg.media_group_id: _last_event_by_media_group[msg.media_group_id] = event_id
        link = f"{BASE_URL}/?focus={event_id}&uid={msg.from_user.id}&sig={sign(msg.from_user.id)}"
        await msg.answer(f"Фото добавлено 🔥\nЖивая карта: {MAP_URL}\nФокус: {link}", reply_markup=_map_button())

    @dp.message(F.text)
    async def maybe_coords(msg: Message):
        now = int(time.time())
        text = (msg.text or "").strip()
        m = COORD_RE.match(text)
        if not m: 
            if text.lower().startswith("contact "):
                # user default contact could be handled here in future
                await msg.answer("Контакт сохранён (локально не сохраняем по пользователю в этой версии).", reply_markup=_map_button())
            return
        lat, lon, tail = float(m.group(1)), float(m.group(2)), (m.group(3) or "").strip()
        is_fire_prefix = text.lower().startswith("fire ")
        is_vol_prefix  = text.lower().startswith("vol ")
        mode = _user_mode.get(msg.from_user.id)
        make_fire = is_fire_prefix or (mode and mode[0] == "report_fire" and now - mode[1] < 1200)
        if is_vol_prefix: make_fire = False
        typ = "fire" if make_fire else "volunteer"
        if make_fire and mode: _user_mode.pop(msg.from_user.id, None)
        _last_location[msg.from_user.id] = (lat, lon, now)
        contact = _tg_contact(msg) if typ == "fire" else None
        event_id = save_event({'ts': now,'type': typ,'lat': lat,'lon': lon,'user_id': msg.from_user.id,
                               'group_id': None,'text': tail or None,'photo_file_id': None,'status': 'active',
                               'contact': contact})
        _last_event_by_user[msg.from_user.id] = (event_id, now)
        link = f"{BASE_URL}/?focus={event_id}&uid={msg.from_user.id}&sig={sign(msg.from_user.id)}"
        gmaps = f"https://www.google.com/maps?q={lat},{lon}"
        await msg.answer(("Очаг зарегистрирован 🔥" if typ=='fire' else "Позиция волонтёра зафиксирована ✅") + f"\nЖивая карта: {MAP_URL}\nФокус: {link}\nGoogle Maps: {gmaps}", reply_markup=_map_button())

    if TELEGRAM_TOKEN:
        asyncio.create_task(dp.start_polling(bot))
    else:
        print("WARNING: TELEGRAM_TOKEN is not set; bot won't start. Map/API still available.")

@app.on_event("startup")
async def _startup():
    init_db()
    await on_startup_polling()
