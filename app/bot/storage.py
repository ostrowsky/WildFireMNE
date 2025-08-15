from __future__ import annotations
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

# =========================== CONFIG ===========================
DB_PATH = os.getenv("DB_PATH", os.path.join(".", "data", "app.db"))

# Глобальное подключение + мьютекс
_conn: Optional[sqlite3.Connection] = None
_lock = threading.RLock()

# =========================== SCHEMA ===========================
_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

/* Точки (волонтёры и очаги) */
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    type            TEXT NOT NULL,           -- 'volunteer' | 'fire'
    lat             REAL,
    lon             REAL,
    user_id         INTEGER,                 -- Telegram user id (owner)
    group_id        INTEGER,                 -- зарезервировано
    text            TEXT,                    -- описание (для очага)
    photo_file_id   TEXT,                    -- фото (для очага)
    status          TEXT DEFAULT 'active',   -- 'active' | 'stopped' | 'deleted'
    contact         TEXT                     -- контакт '@user' или телефон
);

/* Живой трекинг: связь chat_id+msg_id -> текущие координаты  */
CREATE TABLE IF NOT EXISTS live_tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    contact     TEXT,
    started_ts  INTEGER NOT NULL,
    live_until  INTEGER NOT NULL,
    last_ts     INTEGER NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL
);

/* Индексы для быстрого выборочного доступа */
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_live_msg ON live_tracks(chat_id, msg_id);
"""


# =========================== CORE ===========================
def _ensure_folder():
    folder = os.path.dirname(DB_PATH)
    if folder and not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)

def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    _ensure_folder()
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    return _conn

def init_db():
    """Создать файл БД (если не существует) и применить схему."""
    conn = _connect()
    with _lock, conn:
        conn.executescript(_SCHEMA_SQL)

def migrate():
    """Хук на будущее; сейчас схема актуальна."""
    pass


# =========================== WRITE OPS ===========================
def save_event(event: Dict[str, Any]) -> int:
    """
    Добавить точку (волонтёр/очаг).
    Ожидаемые ключи: ts, type, lat, lon, user_id, group_id, text, photo_file_id, status, contact
    Возвращает id добавленной записи.
    """
    conn = _connect()
    with _lock, conn:
        cur = conn.execute(
            """
            INSERT INTO events (ts, type, lat, lon, user_id, group_id, text, photo_file_id, status, contact)
            VALUES (:ts, :type, :lat, :lon, :user_id, :group_id, :text, :photo_file_id, :status, :contact)
            """,
            {
                "ts": event.get("ts"),
                "type": event.get("type"),
                "lat": event.get("lat"),
                "lon": event.get("lon"),
                "user_id": event.get("user_id"),
                "group_id": event.get("group_id"),
                "text": event.get("text"),
                "photo_file_id": event.get("photo_file_id"),
                "status": event.get("status", "active"),
                "contact": event.get("contact"),
            },
        )
        return int(cur.lastrowid)

def add_photo_to_event(event_id: int, file_id: str) -> None:
    """Привязать фото к уже созданному событию (обычно для очага)."""
    conn = _connect()
    with _lock, conn:
        conn.execute(
            "UPDATE events SET photo_file_id = ? WHERE id = ?",
            (file_id, event_id)
        )

def delete_event_by_owner(event_id: int, owner_user_id: int) -> bool:
    """
    Удалить свою точку (мягкое удаление: меняем status=deleted).
    Возвращает True если запись обновлена.
    """
    conn = _connect()
    with _lock, conn:
        cur = conn.execute(
            """
            UPDATE events
               SET status = 'deleted'
             WHERE id = ? AND user_id = ? AND status <> 'deleted'
            """,
            (event_id, owner_user_id),
        )
        return cur.rowcount > 0


# ---------- LIVE TRACK ----------
def upsert_live_event(
    ts: int, user_id: int, lat: float, lon: float,
    contact: Optional[str], chat_id: int, msg_id: int,
    live_until: int
) -> int:
    """
    Старт/апдейт живого трекинга для пары (chat_id, msg_id).
    Возвращает surrogate id live_tracks.
    """
    conn = _connect()
    with _lock, conn:
        # попытка обновить
        cur = conn.execute(
            """
            UPDATE live_tracks
               SET user_id=?, contact=?, started_ts=COALESCE(started_ts, ?),
                   live_until=?, last_ts=?, lat=?, lon=?
             WHERE chat_id=? AND msg_id=?
            """,
            (user_id, contact, ts, live_until, ts, lat, lon, chat_id, msg_id),
        )
        if cur.rowcount > 0:
            # вернуть id
            cur2 = conn.execute(
                "SELECT id FROM live_tracks WHERE chat_id=? AND msg_id=?",
                (chat_id, msg_id)
            )
            row = cur2.fetchone()
            return int(row["id"]) if row else 0

        # иначе вставка
        cur = conn.execute(
            """
            INSERT INTO live_tracks (chat_id, msg_id, user_id, contact, started_ts, live_until, last_ts, lat, lon)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, msg_id, user_id, contact, ts, live_until, ts, lat, lon),
        )
        return int(cur.lastrowid)

def update_live_coords(chat_id: int, msg_id: int, ts: int, lat: float, lon: float) -> None:
    """Обновить координаты живого трека."""
    conn = _connect()
    with _lock, conn:
        conn.execute(
            "UPDATE live_tracks SET last_ts=?, lat=?, lon=? WHERE chat_id=? AND msg_id=?",
            (ts, lat, lon, chat_id, msg_id)
        )

def stop_live(chat_id: int, msg_id: int) -> None:
    """Остановить живой трек (мягко — записи остаются для истории)."""
    conn = _connect()
    with _lock, conn:
        # В простом MVP ничего не меняем, но можно было бы перенести последнюю точку в events
        pass


# =========================== READ OPS ===========================
def _event_row_to_feature(row: sqlite3.Row) -> Dict[str, Any]:
    """events -> GeoJSON Feature"""
    coords = None
    try:
        if row["lat"] is not None and row["lon"] is not None:
            coords = [float(row["lon"]), float(row["lat"])]
    except Exception:
        pass

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": coords} if coords else None,
        "properties": {
            "id": int(row["id"]),
            "ts": int(row["ts"]),
            "type": row["type"],
            "user_id": row["user_id"],
            "group_id": row["group_id"],
            "text": row["text"],
            "photo_file_id": row["photo_file_id"],
            "status": row["status"],
            "contact": row["contact"],
        },
    }

def _live_row_to_feature(row: sqlite3.Row) -> Dict[str, Any]:
    """live_tracks -> GeoJSON Feature (как волонтёр)"""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [float(row["lon"]), float(row["lat"])]},
        "properties": {
            "id": f"live:{int(row['id'])}",
            "ts": int(row["last_ts"]),
            "type": "volunteer",     # для карты — синим
            "user_id": row["user_id"],
            "group_id": None,
            "text": None,
            "photo_file_id": None,
            "status": "active",
            "contact": row["contact"],
        },
    }

def fetch_geojson(limit_latest_per_type: int = 500) -> Dict[str, Any]:
    """
    Собирает все активные точки:
      - events.status != 'deleted'
      - плюс активные live_tracks (live_until >= now)
    Возвращает FeatureCollection. limit_latest_per_type ограничивает верхний предел выборки.
    """
    conn = _connect()
    with _lock:
        # Статические отметки (волонтёры/очаги)
        res1 = conn.execute(
            """
            SELECT * FROM events
             WHERE status <> 'deleted'
             ORDER BY ts DESC
             LIMIT ?
            """,
            (limit_latest_per_type,)
        ).fetchall()

        # Живые треки (ещё не истекли)
        now_ts = int(__import__("time").time())
        res2 = conn.execute(
            """
            SELECT * FROM live_tracks
             WHERE live_until >= ?
             ORDER BY last_ts DESC
             LIMIT ?
            """,
            (now_ts, limit_latest_per_type)
        ).fetchall()

    features: List[Dict[str, Any]] = []
    for r in res1:
        features.append(_event_row_to_feature(r))
    for r in res2:
        features.append(_live_row_to_feature(r))

    # Удалим элементы без координат (например, фото без координат)
    features = [f for f in features if f.get("geometry") and f["geometry"].get("coordinates")]

    return {"type": "FeatureCollection", "features": features}



