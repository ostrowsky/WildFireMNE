import os
import re
import time
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType, ChatType
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)

from app.bot.storage import (
    init_db,
    save_event,           # (type, user_id, username, lat, lon, ts, text, photo_file_id)
    delete_event,
    save_live_start,      # (user_id, username, lat, lon, ts, live_until)
    save_live_update,     # (user_id, lat, lon, ts)
    stop_live             # (user_id)
)

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("TELEGRAM_TOKEN", "")

# aiogram 3.7+: parse_mode через DefaultBotProperties
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ---------------- UI ----------------

def main_menu_kbd() -> ReplyKeyboardMarkup:
    # 4 кнопки как вы требовали
    kb = [
        [KeyboardButton(text="📍 Send my location", request_location=True)],
        [KeyboardButton(text="🟢 Share live location")],
        [KeyboardButton(text="🔥 Report fire")],
        [KeyboardButton(text="🌍 Open live map")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)

def map_button(uid: int, sig: str) -> InlineKeyboardMarkup:
    url = f"{BASE_URL}/?uid={uid}&sig={sig}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌍 Open live map", url=url)]
    ])

def pick_button(uid: int, sig: str) -> InlineKeyboardMarkup:
    url = f"{BASE_URL}/pick?uid={uid}&sig={sig}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Open picker", url=url)]
    ])

# ---------------- FSM ----------------

class AddFire(StatesGroup):
    awaiting_coords = State()
    awaiting_optional_media = State()

# ---------------- HELPERS ----------------

def sign(uid: int) -> str:
    # простая подпись — достаточно для read-only операций удаления собственных точек
    # (совпадает с вашим серверным проверяющим кодом)
    import hashlib
    secret = os.getenv("SECRET_KEY", "dev")
    return hashlib.sha256(f"{uid}:{secret}".encode()).hexdigest()

def parse_coords(text: str) -> Optional[Tuple[float, float]]:
    m = re.search(r"(-?\d+(?:\.\d+)?)[,;\s]+(-?\d+(?:\.\d+)?)", text)
    if not m:
        return None
    lat = float(m.group(1)); lon = float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon

def username_link(m: Message) -> str:
    if m.from_user is None:
        return ""
    if m.from_user.username:
        return f"@{m.from_user.username}"
    return m.from_user.full_name or ""

# ---------------- HANDLERS ----------------

@router.message(CommandStart(), F.chat.type.in_({ChatType.PRIVATE}))
async def on_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "Hi! Choose an action:",
        reply_markup=main_menu_kbd()
    )
    # даём кнопку «Открыть карту» отдельным сообщением (со ссылкой с uid/sig)
    uid = msg.from_user.id
    await msg.answer(
        "Open the live map:",
        reply_markup=map_button(uid, sign(uid))
    )

# 1) одноразовая текущая геолокация — создаём «volunteer»
@router.message(F.content_type == ContentType.LOCATION, F.location.live_period == None)
async def on_location(msg: Message):
    if not msg.location:
        return
    uid = msg.from_user.id
    username = username_link(msg)
    lat = msg.location.latitude
    lon = msg.location.longitude
    ts = int(time.time())

    save_event(
        type="volunteer",
        user_id=uid,
        username=username,
        lat=lat, lon=lon,
        ts=ts,
        text=None,
        photo_file_id=None
    )

    await msg.answer(
        "Location received and added to the map ✅",
        reply_markup=main_menu_kbd()
    )
    # дублируем ссылку на карту
    await msg.answer("Open the live map:", reply_markup=map_button(uid, sign(uid)))

# 2) лайв‑локация — старт
@router.message(F.content_type == ContentType.LOCATION, F.location.live_period.as_('lp'))
async def on_live_start(msg: Message, lp: int):
    # Telegram присылает обычное сообщение с location и live_period > 0 — это старт лайва
    uid = msg.from_user.id
    username = username_link(msg)
    lat = msg.location.latitude
    lon = msg.location.longitude
    ts = int(time.time())
    live_until = ts + int(lp or 0)

    save_live_start(uid=uid, username=username, lat=lat, lon=lon, ts=ts, live_until=live_until)

    await msg.answer(
        "Live location started 🟢 — tracking on the map.",
        reply_markup=main_menu_kbd()
    )
    await msg.answer("Open the live map:", reply_markup=map_button(uid, sign(uid)))

# 2b) лайв‑локация — обновления приходят как edited_message с новым location
@router.edited_message(F.content_type == ContentType.LOCATION)
async def on_live_update(msg: Message):
    if not msg.location:
        return
    uid = msg.from_user.id
    lat = msg.location.latitude
    lon = msg.location.longitude
    ts = int(time.time())
    save_live_update(uid=uid, lat=lat, lon=lon, ts=ts)

# 2c) пользователь остановил live — Telegram пришлёт финальное сообщение без live_period,
#     но точки больше не обновляются. Добавим команду‑кнопку для явной остановки (по желанию).
@router.message(F.text == "🟢 Share live location")
async def explain_live(msg: Message):
    await msg.answer(
        "To share <b>Live Location</b>:\n"
        "• Tap the 📎 (attach) → Location → <b>Share live location</b>\n"
        "I will keep tracking updates automatically.",
        reply_markup=main_menu_kbd()
    )

# 3) Report fire — ведём в пикер с булавкой; после — принимаем координаты/медиа
@router.message(F.text == "🔥 Report fire")
async def report_fire(msg: Message, state: FSMContext):
    await state.set_state(AddFire.awaiting_coords)
    uid = msg.from_user.id
    await msg.answer(
        "Choose the location on the picker and paste coordinates here, "
        "or just send your current location.",
        reply_markup=pick_button(uid, sign(uid))
    )

@router.message(AddFire.awaiting_coords, F.content_type == ContentType.LOCATION)
async def fire_coords_from_location(msg: Message, state: FSMContext):
    lat = msg.location.latitude
    lon = msg.location.longitude
    await state.update_data(coords=(lat, lon))
    await state.set_state(AddFire.awaiting_optional_media)
    await msg.answer("Got coordinates. Now send a photo and/or text (optional), or send “OK” to finish.")

@router.message(AddFire.awaiting_coords, F.text)
async def fire_coords_from_text(msg: Message, state: FSMContext):
    pts = parse_coords(msg.text or "")
    if not pts:
        await msg.answer("Send coordinates like: <code>42.179000, 18.942000</code> "
                         "or share location.")
        return
    await state.update_data(coords=pts)
    await state.set_state(AddFire.awaiting_optional_media)
    await msg.answer("Got coordinates. Now send a photo and/or text (optional), or send “OK” to finish.")

@router.message(AddFire.awaiting_optional_media, F.photo | F.text)
async def fire_finish(msg: Message, state: FSMContext):
    data = await state.get_data()
    coords = data.get("coords")
    if not coords:
        await state.clear()
        await msg.answer("Cancelled.", reply_markup=main_menu_kbd())
        return

    lat, lon = coords
    uid = msg.from_user.id
    username = username_link(msg)
    ts = int(time.time())
    text = None
    file_id = None

    if msg.photo:
        # берём самую крупную
        file_id = msg.photo[-1].file_id
        if msg.caption:
            text = msg.caption
    elif msg.text and msg.text.lower() != "ok":
        text = msg.text

    save_event(
        type="fire",
        user_id=uid,
        username=username,
        lat=lat, lon=lon,
        ts=ts,
        text=text,
        photo_file_id=file_id
    )
    await state.clear()
    await msg.answer("Fire point added 🔥✅", reply_markup=main_menu_kbd())
    await msg.answer("Open the live map:", reply_markup=map_button(uid, sign(uid)))

# 4) открыть карту (обычная, с тайлами)
@router.message(F.text == "🌍 Open live map")
async def open_map(msg: Message):
    uid = msg.from_user.id
    await msg.answer("Open the live map:", reply_markup=map_button(uid, sign(uid)))

# опционально: удаление своей точки по id (адресное удаление идёт со стороны карты по /event/:id)
@router.message(F.text.regexp(r"^/delete\s+\d+$"))
async def cmd_delete(msg: Message):
    m = re.search(r"^/delete\s+(\d+)$", msg.text.strip())
    if not m:
        return
    point_id = int(m.group(1))
    deleted = delete_event(point_id, msg.from_user.id)
    await msg.answer("Deleted ✅" if deleted else "Not found / not yours")

# ---------------- BOOT ----------------

def app_factory():
    # вызывается из asgi/uvicorn
    init_db()
    return dp

# совместимость с Procfile: uvicorn app.bot.main:app
app = app_factory()
