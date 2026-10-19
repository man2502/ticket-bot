"""GET /v2/stations — список станций + локальный кэш."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from railway_client import api_request
from railway_env import RAILWAY_DATA_DIR

# Свежий кэш пишем в каталог данных; список из репозитория — запасной, на
# случай первого запуска в новом месте, пока станции не обновлены.
STATIONS_CACHE_FILE = RAILWAY_DATA_DIR / "railway_stations.json"
BUNDLED_STATIONS_FILE = Path(__file__).resolve().parent / "railway_stations.json"

_STATIONS: list[dict[str, Any]] | None = None
_STATION_NAMES: dict[str, str] | None = None


class StationsError(RuntimeError):
    pass


def fetch_stations() -> list[dict[str, Any]]:
    status, body = api_request("GET", "/v2/stations")
    if status != 200:
        raise StationsError(f"stations → HTTP {status}: {body}")

    if not isinstance(body, dict):
        raise StationsError(f"stations: не JSON-объект: {body!r}")

    if not body.get("success"):
        raise StationsError(f"stations: success=false: {body!r}")

    data = body.get("data")
    if not isinstance(data, dict):
        raise StationsError(f"stations: нет data: {body!r}")

    stations = data.get("stations")
    if not isinstance(stations, list):
        raise StationsError(f"stations: нет data.stations: {body!r}")

    return stations


def station_title(station: dict[str, Any]) -> str:
    return str(
        station.get("title_ru") or station.get("title") or station.get("title_en") or "?"
    )


def format_station(station: dict[str, Any]) -> str:
    station_id = station.get("id", "?")
    title = station_title(station)
    return f"{station_id:>3}  {title}"


def _apply_stations(stations: list[dict[str, Any]]) -> None:
    global _STATIONS, _STATION_NAMES
    _STATIONS = stations
    _STATION_NAMES = {
        str(station.get("id")): station_title(station)
        for station in stations
        if station.get("id") is not None
    }


def save_stations_cache(stations: list[dict[str, Any]]) -> Path:
    payload = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stations": stations,
    }
    STATIONS_CACHE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return STATIONS_CACHE_FILE


def load_stations_cache() -> list[dict[str, Any]]:
    source = STATIONS_CACHE_FILE if STATIONS_CACHE_FILE.exists() else BUNDLED_STATIONS_FILE
    if not source.exists():
        return []

    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StationsError(f"{source.name}: битый JSON: {exc}") from exc

    if isinstance(raw, dict):
        stations = raw.get("stations")
    elif isinstance(raw, list):
        stations = raw
    else:
        raise StationsError(f"{STATIONS_CACHE_FILE.name}: неожиданный формат")

    if not isinstance(stations, list):
        raise StationsError(f"{STATIONS_CACHE_FILE.name}: нет списка stations")

    return [s for s in stations if isinstance(s, dict)]


def refresh_stations_cache(*, verbose: bool = True) -> list[dict[str, Any]]:
    """GET /v2/stations → сохранить в railway_stations.json."""
    try:
        stations = fetch_stations()
    except StationsError as exc:
        cached = load_stations_cache()
        if cached:
            _apply_stations(cached)
            if verbose:
                print(
                    f"[stations] API: {exc} — использую кэш "
                    f"({len(cached)} шт., {STATIONS_CACHE_FILE.name})"
                )
            return cached
        raise

    save_stations_cache(stations)
    _apply_stations(stations)
    if verbose:
        print(f"[stations] обновлено {len(stations)} → {STATIONS_CACHE_FILE.name}")
    return stations


def get_stations() -> list[dict[str, Any]]:
    if _STATIONS is not None:
        return _STATIONS

    cached = load_stations_cache()
    if cached:
        _apply_stations(cached)
        return cached

    return refresh_stations_cache()


def station_names_by_id() -> dict[str, str]:
    if _STATION_NAMES is None:
        get_stations()
    return _STATION_NAMES or {}


def station_label(station_id: str | int | None) -> str:
    sid = str(station_id or "").strip()
    if not sid:
        return "?"
    return station_names_by_id().get(sid) or sid
