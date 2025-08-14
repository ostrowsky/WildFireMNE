import os
import json
import logging
from typing import List

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Wildfire API")

# Читаем переменные окружения
MNE_BBOX = {
    "min_lon": float(os.getenv("MNE_MIN_LON", "18.3")),
    "min_lat": float(os.getenv("MNE_MIN_LAT", "41.8")),
    "max_lon": float(os.getenv("MNE_MAX_LON", "20.4")),
    "max_lat": float(os.getenv("MNE_MAX_LAT", "43.6")),
}
HOTSPOTS_CACHE_SEC = int(os.getenv("HOTSPOTS_CACHE_SEC", "300"))
NASA_API_KEY = os.getenv("NASA_API_KEY", "").strip()

# --- Чтение HOTSPOTS_URLS ---
HOTSPOTS_URLS: List[str] = []
_raw = os.getenv("HOTSPOTS_URLS", "").strip()

if _raw:
    try:
        parsed = json.loads(_raw)
        if isinstance(parsed, list):
            HOTSPOTS_URLS = [u for u in parsed if isinstance(u, str) and u.strip()]
        elif isinstance(parsed, str):
            HOTSPOTS_URLS = [parsed]
    except Exception as e:
        logger.warning(f"Cannot parse HOTSPOTS_URLS={_raw}: {e}")

# --- Fallback: одиночный URL ---
SINGLE_HOTSPOTS_URL = os.getenv("HOTSPOTS_URL", "").strip()
if SINGLE_HOTSPOTS_URL:
    try:
        maybe_list = json.loads(SINGLE_HOTSPOTS_URL)
        if isinstance(maybe_list, list):
            HOTSPOTS_URLS.extend([u for u in maybe_list if isinstance(u, str) and u.strip()])
        elif maybe_list:
            HOTSPOTS_URLS.append(str(maybe_list))
    except Exception:
        HOTSPOTS_URLS.append(SINGLE_HOTSPOTS_URL)

# Удаляем дубли и пустые строки
HOTSPOTS_URLS = list({u.strip() for u in HOTSPOTS_URLS if u.strip()})
logger.info(f"Configured HOTSPOTS_URLS: {HOTSPOTS_URLS}")


@app.get("/hotspots")
async def hotspots(diag: int = 0):
    """
    Возвращает GeoJSON FeatureCollection по активным hotspot-ам.
    Если diag=1 — вернёт статусы загрузки по каждому URL.
    """
    features = []
    diagnostics = []

    async with httpx.AsyncClient(timeout=30) as client:
        for url in HOTSPOTS_URLS:
            try:
                resp = await client.get(url)
                diagnostics.append({"url": url, "status": resp.status_code})
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict) and "features" in data:
                        features.extend(data["features"])
            except Exception as e:
                diagnostics.append({"url": url, "error": str(e)})

    result = {
        "type": "FeatureCollection",
        "features": features,
    }

    if diag:
        return JSONResponse({
            "diagnostics": diagnostics,
            "count_features": len(features),
            "bbox": MNE_BBOX,
        })
    return JSONResponse(result)


@app.get("/hotspots/debug")
async def hotspots_debug():
    """
    Отладочный эндпоинт: какие URL реально видит приложение.
    """
    return JSONResponse({
        "parsed_urls": HOTSPOTS_URLS,
        "use_fallback_firms_api": bool(NASA_API_KEY and not HOTSPOTS_URLS),
        "bbox": MNE_BBOX,
        "cache_sec": HOTSPOTS_CACHE_SEC
    })
