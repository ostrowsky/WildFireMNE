import os
import sqlite3
import logging
from contextlib import contextmanager
from typing import Optional

# Включим простой логгер (Railway подхватит stdout)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Порядок путей: переменная окружения > /data > /workspace/data > /tmp
PRIMARY_DB_PATH = os.getenv("DB_PATH", "/data/app.db")

FALLBACK_PATHS = [
    "/data/app.db",                 # под Volume на Railway
    "/workspace/data/app.db",       # иногда полезно в других контейнерах
    "/tmp/app.db",                  # всегда доступно (но эфемерно)
]


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except Exception as e:
        logging.warning("Cannot create directory %s: %s", parent, e)


def _try_open(path: str) -> Optional[sqlite3.Connection]:
    """
    Пытаемся открыть/создать БД по указанному пути.
    Возвращаем соединение или None при ошибке.
    """
    try:
        _ensure_parent_dir(path)
        # небольшие опции для стабильности
        con = sqlite3.connect(path, timeout=10, check_same_thread=False)
        return con
    except Exception as e:
        logging.warning("DB open failed for %s: %s", path, e)
        return None


def _pick_db_path() -> str:
    # сначала путь из ENV
    candidates = [PRIMARY_DB_PATH] + [p for p in FALLBACK_PATHS if p != PRIMARY_DB_PATH]
    for p in candidates:
        con = _try_open(p)
        if con is not None:
            con.close()
            logging.info("Using SQLite DB at: %s", p)
            return p
    # если совсем ничего не вышло — последний шанс /tmp
    logging.error("All DB locations failed, forcing /tmp/app.db")
    return "/tmp/app.db"


# Выбираем рабочий путь один раз на импорт модуля
DB_PATH = _pick_db_path()


@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with connect() as con:
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,                 -- 'volunteer' | 'fire'
            user_id INTEGER NOT NULL,
            username TEXT,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            ts INTEGER NOT NULL,
            text TEXT,
            photo_file_id TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS live(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            ts INTEGER NOT NULL,
            live_until INTEGER NOT NULL
        )
        """)
        con.commit()
        logging.info("DB ready at %s", DB_PATH)


def save_event(type: str, user_id: int, username: str, lat: float, lon: float,
               ts: int, text: Optional[str], photo_file_id: Optional[str]) -> int:
    with connect() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO events(type,user_id,username,lat,lon,ts,text,photo_file_id) VALUES(?,?,?,?,?,?,?,?)",
            (type, user_id, username, lat, lon, ts, text, photo_file_id)
        )
        return cur.lastrowid


def delete_event(event_id: int, owner_id: int) -> bool:
    with connect() as con:
        cur = con.cursor()
        cur.execute("DELETE FROM events WHERE id=? AND user_id=?", (event_id, owner_id))
        return cur.rowcount > 0


def save_live_start(uid: int, username: str, lat: float, lon: float, ts: int, live_until: int):
    with connect() as con:
        cur = con.cursor()
        cur.execute("""
            INSERT INTO live(user_id, username, lat, lon, ts, live_until)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              lat=excluded.lat, lon=excluded.lon,
              ts=excluded.ts, live_until=excluded.live_until
        """, (uid, username, lat, lon, ts, live_until))


def save_live_update(uid: int, lat: float, lon: float, ts: int):
    with connect() as con:
        cur = con.cursor()
        cur.execute("UPDATE live SET lat=?, lon=?, ts=? WHERE user_id=?", (lat, lon, ts, uid))


def stop_live(uid: int):
    with connect() as con:
        cur = con.cursor()
        cur.execute("DELETE FROM live WHERE user_id=?", (uid,))
