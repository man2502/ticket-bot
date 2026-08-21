"""Тесты синхронизации справочников.

Сеть здесь не трогается: источник подменяется, проверяется поведение записи
в базу. Отдельно проверено, что при недоступном источнике старые записи
остаются на месте — пустой справочник хуже устаревшего, с ним нельзя даже
создать процесс.

Запуск (из корня репозитория):
    python run_tests.py web/test_stations.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Чистая база во временном каталоге."""
    import web.config as config
    import web.crypto as crypto
    import web.db as db_module

    monkeypatch.setenv("TICKET_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(crypto, "SECRET_KEY_FILE", tmp_path / "secret.key")
    monkeypatch.setattr(crypto, "SESSION_KEY_FILE", tmp_path / "session.key")
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_SessionFactory", None)

    db_module.init_db()
    with db_module.session_scope() as session:
        yield session


RAILWAY_ROWS = [
    {"external_id": "17", "title_ru": "Ашхабад", "title_en": "Ashgabat", "code": "17"},
    {"external_id": "49", "title_ru": "Туркменабад", "title_en": "Turkmenabat", "code": "49"},
]


def _patch_source(monkeypatch: pytest.MonkeyPatch, rows_or_error):
    """Подменить источник: список строк или исключение."""
    import web.services.stations as stations

    def fake(_service):
        if isinstance(rows_or_error, Exception):
            raise rows_or_error
        return rows_or_error

    monkeypatch.setattr(stations, "fetch_raw", fake)


# --------------------------------------------------------------------------
# Первичная загрузка
# --------------------------------------------------------------------------


def test_first_sync_adds_everything(db, monkeypatch: pytest.MonkeyPatch) -> None:
    from web.models import Station
    from web.services.stations import sync_service

    _patch_source(monkeypatch, RAILWAY_ROWS)
    result = sync_service(db, "railway")

    assert result.ok is True
    assert (result.added, result.updated, result.total) == (2, 0, 2)
    assert db.query(Station).count() == 2


def test_repeat_sync_changes_nothing(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторное нажатие кнопки не должно плодить дубли."""
    from web.models import Station
    from web.services.stations import sync_service

    _patch_source(monkeypatch, RAILWAY_ROWS)
    sync_service(db, "railway")
    second = sync_service(db, "railway")

    assert (second.added, second.updated, second.deactivated) == (0, 0, 0)
    assert db.query(Station).count() == 2


def test_renamed_station_is_updated(db, monkeypatch: pytest.MonkeyPatch) -> None:
    from web.models import Station
    from web.services.stations import sync_service

    _patch_source(monkeypatch, RAILWAY_ROWS)
    sync_service(db, "railway")

    renamed = [dict(RAILWAY_ROWS[0], title_ru="Ашгабат"), RAILWAY_ROWS[1]]
    _patch_source(monkeypatch, renamed)
    result = sync_service(db, "railway")

    assert result.updated == 1
    station = db.query(Station).filter(Station.external_id == "17").one()
    assert station.title_ru == "Ашгабат"


# --------------------------------------------------------------------------
# Исчезнувшие станции
# --------------------------------------------------------------------------


def test_missing_station_is_hidden_not_deleted(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    На станцию могут ссылаться уже созданные процессы.

    Удалить её значит оставить в базе процесс с маршрутом «ниоткуда никуда»,
    поэтому пропавшая из источника лишь помечается неактивной.
    """
    from web.models import Station
    from web.services.stations import sync_service

    _patch_source(monkeypatch, RAILWAY_ROWS)
    sync_service(db, "railway")

    _patch_source(monkeypatch, [RAILWAY_ROWS[0]])
    result = sync_service(db, "railway")

    assert result.deactivated == 1
    assert db.query(Station).count() == 2  # ничего не удалено

    gone = db.query(Station).filter(Station.external_id == "49").one()
    assert gone.is_active is False


def test_returned_station_becomes_active_again(db, monkeypatch: pytest.MonkeyPatch) -> None:
    from web.models import Station
    from web.services.stations import sync_service

    _patch_source(monkeypatch, RAILWAY_ROWS)
    sync_service(db, "railway")
    _patch_source(monkeypatch, [RAILWAY_ROWS[0]])
    sync_service(db, "railway")

    _patch_source(monkeypatch, RAILWAY_ROWS)
    sync_service(db, "railway")

    back = db.query(Station).filter(Station.external_id == "49").one()
    assert back.is_active is True


# --------------------------------------------------------------------------
# Недоступный источник
# --------------------------------------------------------------------------


def test_network_error_keeps_old_data(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой справочник хуже устаревшего: с ним нельзя создать процесс."""
    from web.models import Station
    from web.services.stations import StationSyncError, sync_service

    _patch_source(monkeypatch, RAILWAY_ROWS)
    sync_service(db, "railway")

    _patch_source(monkeypatch, StationSyncError("сеть недоступна"))
    result = sync_service(db, "railway")

    assert result.ok is False
    assert "сеть" in result.error
    assert db.query(Station).filter(Station.is_active.is_(True)).count() == 2


def test_unexpected_exception_does_not_crash(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Источник чужой — любое его поведение не должно ронять панель."""
    from web.services.stations import sync_service

    _patch_source(monkeypatch, ValueError("что-то совсем неожиданное"))
    result = sync_service(db, "railway")

    assert result.ok is False
    assert "ValueError" in result.error


def test_empty_source_is_treated_as_error(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой ответ — почти наверняка сбой на той стороне, а не «станций больше нет»."""
    from web.models import Station
    from web.services.stations import sync_service

    _patch_source(monkeypatch, RAILWAY_ROWS)
    sync_service(db, "railway")

    _patch_source(monkeypatch, [])
    result = sync_service(db, "railway")

    assert result.ok is False
    assert db.query(Station).filter(Station.is_active.is_(True)).count() == 2


def test_unknown_service_is_rejected(db) -> None:
    from web.services.stations import sync_service

    assert sync_service(db, "автобусы").ok is False


# --------------------------------------------------------------------------
# Разделение сервисов
# --------------------------------------------------------------------------


def test_services_do_not_touch_each_other(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Синхронизация поездов не должна прятать аэропорты.

    Обе записи лежат в одной таблице, и фильтр по сервису здесь — единственное,
    что их разделяет.
    """
    import web.services.stations as stations
    from web.models import Station

    def fake(service):
        if service == "railway":
            return RAILWAY_ROWS
        return [{"external_id": "ASB", "code": "ASB", "title_ru": "Ашхабад", "title_en": "Ashgabat"}]

    monkeypatch.setattr(stations, "fetch_raw", fake)

    stations.sync_service(db, "airlines")
    stations.sync_service(db, "railway")

    assert db.query(Station).filter(Station.service == "airlines").count() == 1
    assert db.query(Station).filter(Station.is_active.is_(True)).count() == 3


def test_same_code_in_two_services_is_allowed(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Уникальность — по паре (сервис, код), а не по коду."""
    import web.services.stations as stations
    from web.models import Station

    row = [{"external_id": "17", "code": "17", "title_ru": "Что-то", "title_en": ""}]
    monkeypatch.setattr(stations, "fetch_raw", lambda _s: row)

    stations.sync_service(db, "railway")
    stations.sync_service(db, "airlines")

    assert db.query(Station).filter(Station.external_id == "17").count() == 2


# --------------------------------------------------------------------------
# Первичное наполнение при старте
# --------------------------------------------------------------------------


def test_ensure_stations_fills_empty(db, monkeypatch: pytest.MonkeyPatch) -> None:
    import web.services.stations as stations
    from web.models import Station

    monkeypatch.setattr(stations, "fetch_raw", lambda _s: RAILWAY_ROWS)
    stations.ensure_stations(db, verbose=False)

    assert db.query(Station).count() == 4  # по два на каждый сервис


def test_ensure_stations_does_not_refetch(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Уже наполненный справочник при старте не трогаем: это лишний поход в сеть."""
    import web.services.stations as stations

    calls: list[str] = []

    def counting(service):
        calls.append(service)
        return RAILWAY_ROWS

    monkeypatch.setattr(stations, "fetch_raw", counting)
    stations.ensure_stations(db, verbose=False)
    calls.clear()

    stations.ensure_stations(db, verbose=False)
    assert calls == []


def test_ensure_stations_survives_dead_source(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Без сети панель обязана подняться — справочник обновят кнопкой."""
    import web.services.stations as stations

    monkeypatch.setattr(
        stations, "fetch_raw", lambda _s: (_ for _ in ()).throw(OSError("нет сети"))
    )
    stations.ensure_stations(db, verbose=False)  # не должно бросить


# --------------------------------------------------------------------------
# Текст результата
# --------------------------------------------------------------------------


def test_summary_mentions_changes() -> None:
    from web.services.stations import SyncResult

    text = SyncResult(service="railway", added=3, deactivated=1, total=142).summary()
    assert "142" in text and "3" in text and "1" in text


def test_summary_says_when_nothing_changed() -> None:
    from web.services.stations import SyncResult

    assert "изменений нет" in SyncResult(service="railway", total=142).summary()


def test_summary_shows_error() -> None:
    from web.services.stations import SyncResult

    assert "Ошибка" in SyncResult(service="railway", error="таймаут").summary()
