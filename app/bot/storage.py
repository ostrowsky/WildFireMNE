import os
import sqlite3
from contextlib import contextmanager
from typing import Optional, Dict, Any

DB_PATH = os.getenv("DB_PATH", "/data/app.db")

@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH)
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
