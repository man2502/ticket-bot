"""Синхронизация справочников: станции поездов и аэропорты.

Зачем держать их локально. Форма создания процесса должна открываться мгновенно
и работать, когда сайт перевозчика лежит, — а лежит он регулярно, и именно
в такие моменты билеты и ищут. Плюс список станций меняется раз в год, дёргать
его на каждый показ формы незачем.

Источники:

* поезда — `GET /v2/stations` того же API, что использует бот
  (переиспользуем `railway_stations.fetch_stations`, чтобы разбор ответа был
  ровно один на проект);
* самолёты — `GET /ru/get_internal_ports` с сайта. В iOS-API аэропортов нет,
  а в браузерном боте этот адрес дёргается через `page.evaluate`. Проверено:
  обычный HTTPS-запрос без авторизации отдаёт то же самое, браузер не нужен.

Исчезнувшие станции не удаляются, а помечаются `is_active=False`: на них могут
ссылаться уже созданные процессы, и удаление оставило бы в базе процесс
с маршрутом «ниоткуда никуда».
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RAILWAY_DIR = REPO_ROOT / "railway-api"
for _path in (REPO_ROOT, RAILWAY_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.http import HttpClient, RequestFailed  # noqa: E402

from web.models import Service, Station  # noqa: E402

__all__ = ["SyncResult", "sync_service", "sync_all", "fetch_raw", "StationSyncError"]

AIRLINES_BASE_URL = "https://turkmenistanairlines.tm"
AIRLINES_PORTS_PATH = "/ru/get_internal_ports"


class StationSyncError(RuntimeError):
    """Источник недоступен или ответил не тем, чего мы ждали."""


@dataclass
class SyncResult:
    """Что изменилось за одну синхронизацию — показывается человеку после нажатия."""

    service: str
    added: int = 0
    updated: int = 0
    deactivated: int = 0
    total: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def summary(self) -> str:
        if self.error:
            return f"Ошибка: {self.error}"
        parts = [f"всего {self.total}"]
        if self.added:
            parts.append(f"добавлено {self.added}")
        if self.updated:
            parts.append(f"обновлено {self.updated}")
        if self.deactivated:
            parts.append(f"скрыто {self.deactivated}")
        if len(parts) == 1:
            parts.append("изменений нет")
        return ", ".join(parts)


# --------------------------------------------------------------------------- #
# Получение данных из источников
# --------------------------------------------------------------------------- #


def _fetch_railway() -> list[dict[str, Any]]:
    """
    Станции поездов через тот же модуль, которым пользуется бот.

    Импорт внутри функции: `railway_stations` тянет за собой HTTP-клиент
    и настройки, и делать это при старте веб-панели, которая может никогда
    не открыть раздел справочников, незачем.
    """
    from railway_stations import StationsError, fetch_stations  # noqa: PLC0415

    try:
        stations = fetch_stations()
    except StationsError as exc:
        raise StationSyncError(str(exc)) from exc

    rows: list[dict[str, Any]] = []
    for station in stations:
        external_id = str(station.get("id") or "").strip()
        if not external_id:
            continue
        rows.append(
            {
                "external_id": external_id,
                "title_ru": str(station.get("title_ru") or station.get("title") or ""),
                "title_en": str(station.get("title_en") or ""),
                # У станций нет короткого кода — показываем номер, он же
                # уходит в SOURCE_STATION_ID при запуске бота.
                "code": external_id,
            }
        )
    return rows


def _fetch_airlines() -> list[dict[str, Any]]:
    """Аэропорты с сайта. Формат: {id, cityname, portname, portcode, countrycode}."""
    client = HttpClient(
        AIRLINES_BASE_URL,
        headers_factory=lambda: {
            "accept": "application/json",
            # Без узнаваемого user-agent сайт иногда отвечает страницей-заглушкой.
            "user-agent": "Mozilla/5.0 (compatible; TicketPanel/1.0)",
        },
        connect_timeout=5.0,
        read_timeout=15.0,
        name="airlines-ports",
    )

    try:
        response = client.request("GET", AIRLINES_PORTS_PATH)
    except RequestFailed as exc:
        raise StationSyncError(f"сеть: {exc}") from exc
    finally:
        client.close()

    if response.status != 200:
        raise StationSyncError(f"HTTP {response.status}: {response.safe_text(200)}")

    body = response.json
    if not isinstance(body, dict) or not body.get("success"):
        raise StationSyncError(f"неожиданный ответ: {response.safe_text(200)}")

    data = body.get("data")
    if not isinstance(data, list):
        raise StationSyncError("в ответе нет списка портов")

    rows: list[dict[str, Any]] = []
    for port in data:
        if not isinstance(port, dict):
            continue
        code = str(port.get("portcode") or "").strip().upper()
        if not code:
            continue
        city = str(port.get("cityname") or port.get("portname") or "").strip()
        rows.append(
            {
                # Ключ — код порта: именно он уходит в DEPART_PORT_CODE бота,
                # а числовой id сайта в запросах не используется.
                "external_id": code,
                "code": code,
                "title_ru": city,
                "title_en": str(port.get("portname") or city),
            }
        )
    return rows


_FETCHERS = {
    Service.RAILWAY.value: _fetch_railway,
    Service.AIRLINES.value: _fetch_airlines,
}


def fetch_raw(service: str) -> list[dict[str, Any]]:
    """Забрать справочник из источника, ничего не записывая. Для тестов и отладки."""
    fetcher = _FETCHERS.get(service)
    if fetcher is None:
        raise StationSyncError(f"неизвестный сервис: {service!r}")
    return fetcher()


# --------------------------------------------------------------------------- #
# Запись в базу
# --------------------------------------------------------------------------- #


def sync_service(db, service: str) -> SyncResult:
    """
    Обновить справочник одного сервиса.

    Сеть может не ответить — это штатная ситуация, а не ошибка приложения:
    возвращаем результат с текстом ошибки, а старые записи оставляем на месте.
    Пустой справочник хуже устаревшего: с ним нельзя даже создать процесс.
    """
    result = SyncResult(service=service)

    try:
        rows = fetch_raw(service)
    except StationSyncError as exc:
        result.error = str(exc)
        return result
    except Exception as exc:  # noqa: BLE001 — источник чужой, ронять панель нельзя
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    if not rows:
        result.error = "источник вернул пустой список"
        return result

    existing = {
        station.external_id: station
        for station in db.query(Station).filter(Station.service == service).all()
    }
    now = dt.datetime.now()
    seen: set[str] = set()

    for row in rows:
        external_id = row["external_id"]
        seen.add(external_id)
        station = existing.get(external_id)

        if station is None:
            db.add(Station(service=service, synced_at=now, is_active=True, **row))
            result.added += 1
            continue

        changed = any(
            getattr(station, field) != value for field, value in row.items()
        ) or not station.is_active

        for field, value in row.items():
            setattr(station, field, value)
        station.is_active = True
        station.synced_at = now
        if changed:
            result.updated += 1

    # Пропавшее из источника прячем, но не удаляем: на него могут ссылаться
    # уже созданные процессы.
    for external_id, station in existing.items():
        if external_id not in seen and station.is_active:
            station.is_active = False
            station.synced_at = now
            result.deactivated += 1

    db.commit()
    result.total = len(rows)
    return result


def sync_all(db) -> list[SyncResult]:
    """Обновить оба справочника. Падение одного не мешает другому."""
    return [sync_service(db, service.value) for service in Service]


def ensure_stations(db, *, verbose: bool = True) -> None:
    """
    Первичное наполнение: тянем справочник, только если он пуст.

    Вызывается при старте панели. Ошибку не поднимаем — без сети панель обязана
    подняться, а справочник человек обновит кнопкой, когда связь появится.
    """
    for service in Service:
        has_any = (
            db.query(Station).filter(Station.service == service.value).first() is not None
        )
        if has_any:
            continue
        result = sync_service(db, service.value)
        if verbose:
            state = result.summary() if result.ok else f"не удалось — {result.error}"
            print(f"[stations] {service.value}: {state}")
