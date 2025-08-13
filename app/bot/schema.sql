CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  type TEXT NOT NULL CHECK (type in ('volunteer','fire','safe','note')),
  lat REAL,
  lon REAL,
  user_id INTEGER,
  group_id TEXT,
  text TEXT,
  photo_file_id TEXT,
  status TEXT DEFAULT 'active' CHECK (status in ('active','verified','resolved')),
  contact TEXT
);
CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  ts INTEGER NOT NULL,
  file_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_geo ON events(lat, lon);
CREATE INDEX IF NOT EXISTS idx_photos_event ON photos(event_id, ts DESC);
