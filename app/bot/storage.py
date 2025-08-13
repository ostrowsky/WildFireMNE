from __future__ import annotations
import os
import sqlite3
from typing import Any, Dict

DB_PATH = os.getenv(
    "DB_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "wildfire.db"))
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    type TEXT NOT NULL,              -- 'fire' | 'volunteer'
    lat REAL,
    lon REAL,
    user_id INTEGER,
    group_id INTEGER,
    text TEXT,
    photo_file_id TEXT,
    status TEXT DEFAULT 'active',
    contact TEXT,
    msg_chat_id INTEGER,
    msg_id INTEGER,
    is_live INTEGER DEFAULT 0,
    live_until INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_coords ON events(lat, lon);
CREATE INDEX IF NOT EXISTS idx_events_live ON events(is_live, msg_chat_id, msg_id);

CREATE TABLE IF NOT EXISTS photos(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_photos_event ON photos(event_id);
"""

def init_db():
    with sqlite3.connect(DB_PATH) as c:
        for stmt in SCHEMA_SQL.strip().split(";\n"):
            s = stmt.strip()
            if s:
                c.execute(s)

def migrate():
    with sqlite3.connect(DB_PATH) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(events)")}
        if "contact" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN contact TEXT")
        if "msg_chat_id" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN msg_chat_id INTEGER")
        if "msg_id" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN msg_id INTEGER")
        if "is_live" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN is_live INTEGER DEFAULT 0")
        if "live_until" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN live_until INTEGER")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_live ON events(is_live, msg_chat_id, msg_id)")

def save_event(e: Dict[str, Any]) -> int:
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO events(ts,type,lat,lon,user_id,group_id,text,photo_file_id,status,contact,msg_chat_id,msg_id,is_live,live_until) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(e["ts"]), e["type"], e.get("lat"), e.get("lon"),
                e.get("user_id"), e.get("group_id"), e.get("text"),
                e.get("photo_file_id"), e.get("status", "active"),
                e.get("contact"), e.get("msg_chat_id"), e.get("msg_id"),
                1 if e.get("is_live") else 0, e.get("live_until"),
            ),
        )
        return cur.lastrowid

def upsert_live_event(*, ts:int, user_id:int, lat:float, lon:float, contact:str|None,
                      chat_id:int, msg_id:int, live_until:int) -> int:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT id FROM events WHERE is_live=1 AND msg_chat_id=? AND msg_id=? LIMIT 1",
            (chat_id, msg_id)
        ).fetchone()
        if row:
            eid = int(row[0])
            c.execute(
                "UPDATE events SET ts=?, lat=?, lon=?, user_id=?, contact=?, live_until=?, status='active' WHERE id=?",
                (ts, lat, lon, user_id, contact, live_until, eid)
            )
            return eid
        cur = c.execute(
            "INSERT INTO events(ts,type,lat,lon,user_id,group_id,text,photo_file_id,status,contact,msg_chat_id,msg_id,is_live,live_until) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, "volunteer", lat, lon, user_id, None, None, None, "active", contact, chat_id, msg_id, 1, live_until)
        )
        return cur.lastrowid

def update_live_coords(chat_id:int, msg_id:int, *, ts:int, lat:float, lon:float):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "UPDATE events SET ts=?, lat=?, lon=? WHERE is_live=1 AND msg_chat_id=? AND msg_id=?",
            (ts, lat, lon, chat_id, msg_id)
        )

def stop_live(chat_id:int, msg_id:int):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "UPDATE events SET is_live=0 WHERE is_live=1 AND msg_chat_id=? AND msg_id=?",
            (chat_id, msg_id)
        )

def add_photo_to_event(event_id: int, file_id: str):
    with sqlite3.connect(DB_PATH) as c:
        c.execute("INSERT INTO photos(event_id,file_id) VALUES(?,?)", (event_id, file_id))

def delete_event_by_owner(event_id: int, owner_user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute("SELECT user_id FROM events WHERE id=?", (event_id,))
        row = cur.fetchone()
        if not row or int(row[0] or 0) != int(owner_user_id):
            return False
        c.execute("DELETE FROM events WHERE id=?", (event_id,))
        c.execute("DELETE FROM photos WHERE event_id=?", (event_id,))
        return True

def fetch_geojson() -> Dict[str, Any]:
    features = []
    with sqlite3.connect(DB_PATH) as c:
        for row in c.execute(
            """
            SELECT e.id, e.ts, e.type, e.lat, e.lon, e.text, e.status, e.contact, e.user_id,
                   e.is_live, e.live_until,
                   (SELECT COUNT(1) FROM photos p WHERE p.event_id=e.id) AS photo_count,
                   (SELECT p.file_id FROM photos p WHERE p.event_id=e.id ORDER BY p.id DESC LIMIT 1) AS last_photo
            FROM events e
            WHERE e.lat IS NOT NULL AND e.lon IS NOT NULL
            ORDER BY e.ts DESC
            LIMIT 5000
            """
        ):
            (fid, ts, typ, lat, lon, text, status, contact, user_id,
             is_live, live_until, photo_count, last_photo) = row
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "id": str(fid),
                        "ts": int(ts),
                        "type": typ,
                        "text": text,
                        "status": status,
                        "photos": int(photo_count),
                        "photo_file_id": last_photo,
                        "contact": contact,
                        "user_id": int(user_id) if user_id is not None else None,
                        "live": bool(is_live),
                        "live_until": int(live_until) if live_until else None,
                    },
                    "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                }
            )
    return {"type": "FeatureCollection", "features": features}
