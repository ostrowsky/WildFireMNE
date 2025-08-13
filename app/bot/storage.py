import os, sqlite3
from typing import Dict, Any, List, Optional, Tuple

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data.db"))

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as c, open(os.path.join(os.path.dirname(__file__), "schema.sql"), "r", encoding="utf-8") as f:
        c.executescript(f.read())

def save_event(e: Dict[str, Any]) -> int:
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO events(ts,type,lat,lon,user_id,group_id,text,photo_file_id,status,contact) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (e['ts'], e['type'], e.get('lat'), e.get('lon'), e.get('user_id'),
             e.get('group_id'), e.get('text'), e.get('photo_file_id'),
             e.get('status','active'), e.get('contact'))
        )
        return cur.lastrowid

def add_photo(event_id: int, ts: int, file_id: str) -> int:
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute("INSERT INTO photos(event_id, ts, file_id) VALUES(?,?,?)", (event_id, ts, file_id))
        return cur.lastrowid

def get_photo(photo_id: int):
    with sqlite3.connect(DB_PATH) as c:
        return c.execute("SELECT id, event_id, ts, file_id FROM photos WHERE id=?", (photo_id,)).fetchone()

def get_photos(event_id: int):
    with sqlite3.connect(DB_PATH) as c:
        return c.execute("SELECT id, event_id, ts, file_id FROM photos WHERE event_id=? ORDER BY ts ASC", (event_id,)).fetchall()

def get_event(event_id: int):
    with sqlite3.connect(DB_PATH) as c:
        return c.execute("""SELECT id,ts,type,lat,lon,user_id,group_id,text,photo_file_id,status,contact
                          FROM events WHERE id=?""", (event_id,)).fetchone()

def delete_event_for_user(event_id: int, user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT user_id FROM events WHERE id=?", (event_id,)).fetchone()
        if not row or row[0] != user_id:
            return False
        c.execute("DELETE FROM events WHERE id=?", (event_id,))
        return True

def fetch_geojson() -> Dict[str, Any]:
    features: List[Dict[str, Any]] = []
    with sqlite3.connect(DB_PATH) as c:
        for row in c.execute("""SELECT e.id,e.ts,e.type,e.lat,e.lon,e.text,e.status,e.contact,e.user_id,
                                      (SELECT COUNT(1) FROM photos p WHERE p.event_id=e.id) AS photo_count
                               FROM events e
                               WHERE e.lat IS NOT NULL AND e.lon IS NOT NULL
                               ORDER BY e.ts DESC LIMIT 5000"""):
            fid, ts, typ, lat, lon, text, status, contact, user_id, photo_count = row
            features.append({
                'type': 'Feature',
                'properties': {'id': str(fid), 'ts': ts, 'type': typ, 'text': text, 'status': status,
                               'photos': int(photo_count), 'contact': contact, 'user_id': user_id},
                'geometry': {'type': 'Point', 'coordinates': [lon, lat]}
            })
    return {'type': 'FeatureCollection', 'features': features}
