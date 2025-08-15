import os
import time
import threading
from typing import Any, Dict, List, Optional

# --- DB selection ---
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = os.getenv("DB_PATH", "./app.db")
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
_lock = threading.RLock()

if USE_PG:
    import psycopg2

    def _connect():
        return psycopg2.connect(DATABASE_URL, sslmode=os.getenv("PGSSLMODE", "require"))
else:
    import sqlite3

    def _connect():
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn


class Storage:
    def __init__(self) -> None:
        self.pg = USE_PG
        self.conn = _connect()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create tables if not exist: events, live_tracks"""
        if self.pg:
            with self.conn, self.conn.cursor() as cur:
                # static events (volunteer/fire)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS events(
                  id BIGSERIAL PRIMARY KEY,
                  type VARCHAR(16) NOT NULL,
                  lat DOUBLE PRECISION NOT NULL,
                  lon DOUBLE PRECISION NOT NULL,
                  user_id BIGINT,
                  contact TEXT,
                  text TEXT,
                  photo_file_id TEXT,
                  ts BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW()))
                );""")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);")
                # live tracking per user
                cur.execute("""
                CREATE TABLE IF NOT EXISTS live_tracks(
                  user_id BIGINT PRIMARY KEY,
                  contact TEXT,
                  lat DOUBLE PRECISION NOT NULL,
                  lon DOUBLE PRECISION NOT NULL,
                  live_until BIGINT NOT NULL,
                  updated_ts BIGINT NOT NULL
                );""")
        else:
            with self.conn:
                self.conn.execute("""
                CREATE TABLE IF NOT EXISTS events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  type TEXT NOT NULL,
                  lat REAL NOT NULL,
                  lon REAL NOT NULL,
                  user_id INTEGER,
                  contact TEXT,
                  text TEXT,
                  photo_file_id TEXT,
                  ts INTEGER NOT NULL DEFAULT (strftime('%s','now'))
                );""")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);")
                self.conn.execute("""
                CREATE TABLE IF NOT EXISTS live_tracks(
                  user_id INTEGER PRIMARY KEY,
                  contact TEXT,
                  lat REAL NOT NULL,
                  lon REAL NOT NULL,
                  live_until INTEGER NOT NULL,
                  updated_ts INTEGER NOT NULL
                );""")

    # ---------- events (static points) ----------
    def save_event(
        self,
        etype: str,
        lat: float,
        lon: float,
        user_id: Optional[int] = None,
        contact: Optional[str] = None,
        text: Optional[str] = None,
        photo_file_id: Optional[str] = None,
        ts: Optional[int] = None,
    ) -> int:
        if ts is None:
            ts = int(time.time())
        with _lock:
            if self.pg:
                with self.conn, self.conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO events(type,lat,lon,user_id,contact,text,photo_file_id,ts)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id;""",
                        (etype, lat, lon, user_id, contact, text, photo_file_id, ts),
                    )
                    return int(cur.fetchone()[0])
            else:
                cur = self.conn.cursor()
                cur.execute(
                    """INSERT INTO events(type,lat,lon,user_id,contact,text,photo_file_id,ts)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (etype, lat, lon, user_id, contact, text, photo_file_id, ts),
                )
                self.conn.commit()
                return int(cur.lastrowid)

    def remove_event(self, event_id: int, user_id: Optional[int]) -> bool:
        if user_id is None:
            return False
        with _lock:
            if self.pg:
                with self.conn, self.conn.cursor() as cur:
                    cur.execute("DELETE FROM events WHERE id=%s AND user_id=%s", (event_id, user_id))
                    return cur.rowcount > 0
            else:
                cur = self.conn.cursor()
                cur.execute("DELETE FROM events WHERE id=? AND user_id=?", (event_id, user_id))
                self.conn.commit()
                return cur.rowcount > 0

    def list_events(self) -> List[Dict[str, Any]]:
        if self.pg:
            with self.conn.cursor() as cur:
                cur.execute("SELECT id,type,lat,lon,user_id,contact,text,photo_file_id,ts FROM events ORDER BY ts DESC;")
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        else:
            cur = self.conn.cursor()
            cur.execute("SELECT id,type,lat,lon,user_id,contact,text,photo_file_id,ts FROM events ORDER BY ts DESC;")
            return [dict(r) for r in cur.fetchall()]

    # ---------- live tracks ----------
    def upsert_live_start(self, user_id: int, contact: str, lat: float, lon: float, live_until: int) -> None:
        now = int(time.time())
        with _lock:
            if self.pg:
                with self.conn, self.conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO live_tracks(user_id,contact,lat,lon,live_until,updated_ts)
                        VALUES (%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (user_id) DO UPDATE
                          SET contact=EXCLUDED.contact, lat=EXCLUDED.lat, lon=EXCLUDED.lon,
                              live_until=EXCLUDED.live_until, updated_ts=EXCLUDED.updated_ts;
                    """, (user_id, contact, lat, lon, live_until, now))
            else:
                cur = self.conn.cursor()
                cur.execute("""
                    INSERT INTO live_tracks(user_id,contact,lat,lon,live_until,updated_ts)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET
                      contact=excluded.contact, lat=excluded.lat, lon=excluded.lon,
                      live_until=excluded.live_until, updated_ts=excluded.updated_ts
                """, (user_id, contact, lat, lon, live_until, now))
                self.conn.commit()

    def update_live_position(self, user_id: int, lat: float, lon: float) -> None:
        now = int(time.time())
        with _lock:
            if self.pg:
                with self.conn, self.conn.cursor() as cur:
                    cur.execute("UPDATE live_tracks SET lat=%s, lon=%s, updated_ts=%s WHERE user_id=%s",
                                (lat, lon, now, user_id))
            else:
                cur = self.conn.cursor()
                cur.execute("UPDATE live_tracks SET lat=?, lon=?, updated_ts=? WHERE user_id=?",
                            (lat, lon, now, user_id))
                self.conn.commit()

    def remove_live(self, user_id: int) -> bool:
        with _lock:
            if self.pg:
                with self.conn, self.conn.cursor() as cur:
                    cur.execute("DELETE FROM live_tracks WHERE user_id=%s", (user_id,))
                    return cur.rowcount > 0
            else:
                cur = self.conn.cursor()
                cur.execute("DELETE FROM live_tracks WHERE user_id=?", (user_id,))
                self.conn.commit()
                return cur.rowcount > 0

    def purge_expired_live(self, now_ts: Optional[int] = None) -> int:
        if now_ts is None:
            now_ts = int(time.time())
        with _lock:
            if self.pg:
                with self.conn, self.conn.cursor() as cur:
                    cur.execute("DELETE FROM live_tracks WHERE live_until < %s", (now_ts,))
                    return cur.rowcount
            else:
                cur = self.conn.cursor()
                cur.execute("DELETE FROM live_tracks WHERE live_until < ?", (now_ts,))
                self.conn.commit()
                return cur.rowcount

    def list_live(self, active_only: bool = True) -> List[Dict[str, Any]]:
        now = int(time.time())
        if self.pg:
            with self.conn.cursor() as cur:
                if active_only:
                    cur.execute("SELECT user_id,contact,lat,lon,live_until,updated_ts FROM live_tracks WHERE live_until >= %s", (now,))
                else:
                    cur.execute("SELECT user_id,contact,lat,lon,live_until,updated_ts FROM live_tracks")
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        else:
            cur = self.conn.cursor()
            if active_only:
                cur.execute("SELECT user_id,contact,lat,lon,live_until,updated_ts FROM live_tracks WHERE live_until >= ?", (now,))
            else:
                cur.execute("SELECT user_id,contact,lat,lon,live_until,updated_ts FROM live_tracks")
            return [dict(r) for r in cur.fetchall()]

    # ---------- GeoJSON ----------
    @staticmethod
    def _feat_event(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(row["lon"]), float(row["lat"])]},
            "properties": {
                "id": int(row["id"]),
                "type": str(row["type"]),
                "user_id": int(row["user_id"]) if row.get("user_id") is not None else None,
                "contact": row.get("contact"),
                "text": row.get("text"),
                "photo_file_id": row.get("photo_file_id"),
                "ts": int(row["ts"]) if row.get("ts") is not None else None,
            },
        }

    @staticmethod
    def _feat_live(row: Dict[str, Any]) -> Dict[str, Any]:
        fid = -int(row["user_id"])  # stable negative id
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(row["lon"]), float(row["lat"])]},
            "properties": {
                "id": fid,
                "type": "volunteer_live",
                "user_id": int(row["user_id"]),
                "contact": row.get("contact"),
                "text": None,
                "photo_file_id": None,
                "ts": int(row["updated_ts"]) if row.get("updated_ts") is not None else None,
                "live_until": int(row["live_until"]) if row.get("live_until") is not None else None,
            },
        }

    def geojson(self) -> Dict[str, Any]:
        features: List[Dict[str, Any]] = [self._feat_event(r) for r in self.list_events()]
        for r in self.list_live(active_only=True):
            features.append(self._feat_live(r))
        return {"type": "FeatureCollection", "features": features}


# --- module level API ---
_storage: Optional[Storage] = None

def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage

def init_db() -> None:
    get_storage()

def save_event(**kwargs) -> int:
    return get_storage().save_event(**kwargs)

def remove_event(event_id: int, user_id: Optional[int]) -> bool:
    return get_storage().remove_event(event_id, user_id)

def remove_live(user_id: int) -> bool:
    return get_storage().remove_live(user_id)

def update_live_position(user_id: int, lat: float, lon: float) -> None:
    return get_storage().update_live_position(user_id, lat, lon)

def upsert_live_start(user_id: int, contact: str, lat: float, lon: float, live_until: int) -> None:
    return get_storage().upsert_live_start(user_id, contact, lat, lon, live_until)

def purge_expired_live(now_ts: Optional[int] = None) -> int:
    return get_storage().purge_expired_live(now_ts)

def get_geojson() -> Dict[str, Any]:
    return get_storage().geojson()

# --- Backward-compat aliases ---
def add_event(*args, **kwargs):
    return save_event(*args, **kwargs)

def delete_event(*args, **kwargs):
    return remove_event(*args, **kwargs)
