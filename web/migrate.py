"""Переезд панели на другую машину: архив с данными и правка путей после распаковки.

Старая машина (Windows):
    python -m web.cli export-data --out ticket-data.tar.gz

Новая (контейнер): архив распаковывается в том /data, а при старте контейнера
`relocate-paths --if-needed` один раз переписывает в базе пути старой машины.

Раскладка архива — ровно раскладка тома /data в контейнере:

    web/                    база, ключи шифрования, аккаунты (TICKET_DATA_DIR)
    captcha_dataset_api/    набор капч для обучения (CAPTCHA_API_DATASET_DIR)
    model/                  модель капчи и её прошлая версия (CAPTCHA_API_CNN_MODEL)
    railway/                журнал броней поезда, состояние, станции (RAILWAY_DATA_DIR)
    MANIFEST.json           откуда архив: корень проекта на старой машине

Чего в архиве нет и почему:
    web/runs/               check.env с паролями; панель создаёт их заново при запуске
    airlines_api/.env.local старый общий файл; у каждого аккаунта давно свой
"""

from __future__ import annotations

import datetime as dt
import io
import json
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Iterator

from web import config

__all__ = ["export_data", "relocate_paths", "MANIFEST_NAME"]

MANIFEST_NAME = "MANIFEST.json"
# Отметка «пути уже переписаны» — чтобы перезапуск контейнера не делал это снова.
RELOCATED_MARK = ".relocated"

# Файлы в каталоге данных, которые не переносим.
_SKIP_IN_DATA = {"runs"}
_DB_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _dataset_dir() -> Path:
    if str(config.AIRLINES_API_DIR) not in sys.path:
        sys.path.insert(0, str(config.AIRLINES_API_DIR))
    import captcha_dataset_api  # noqa: PLC0415 — модуль бота, путь из его окружения

    return Path(captcha_dataset_api.DATASET_DIR)


def _model_files() -> list[Path]:
    """Рабочая модель, прошлая версия и их метрики. Кандидат не нужен — его переобучат."""
    from web.services.training import LIVE_MODEL  # noqa: PLC0415

    folder = LIVE_MODEL.parent
    return sorted(
        path
        for path in folder.glob("captcha_model_api*")
        if path.is_file() and ".candidate." not in path.name and not path.name.endswith(".tmp")
    )


def _railway_files() -> list[Path]:
    names = ("railway_bookings.json", "railway_state.json", "railway_stations.json")
    return [config.RAILWAY_DATA_DIR / name for name in names if (config.RAILWAY_DATA_DIR / name).exists()]


def _walk(folder: Path, *, skip: set[str] = frozenset()) -> Iterator[Path]:
    if not folder.exists():
        return
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if rel.parts and rel.parts[0] in skip:
            continue
        if path.is_file():
            yield path


def _db_snapshot() -> Path:
    """
    Копия базы через backup API SQLite.

    Просто скопировать файл нельзя: база в режиме WAL, и последние записи
    лежат в ticket.db-wal. Копия без него молча теряет их.
    """
    source = config.db_path()
    target = Path(tempfile.mkdtemp(prefix="ticket-export-")) / source.name
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def export_data(out: Path, *, include_dataset: bool = True) -> dict:
    """Собрать архив с данными. Возвращает сводку: что и сколько вошло."""
    db_name = config.db_path().name
    db_files = {db_name + suffix for suffix in _DB_SUFFIXES}
    summary = {"web": 0, "captcha_dataset_api": 0, "model": 0, "railway": 0}

    snapshot = _db_snapshot()
    out.parent.mkdir(parents=True, exist_ok=True)
    # PNG почти не сжимаются: сильное сжатие только тратит время.
    with tarfile.open(out, "w:gz", compresslevel=1) as tar:
        tar.add(snapshot, arcname=f"web/{db_name}")
        summary["web"] += 1
        for path in _walk(config.DATA_DIR, skip=_SKIP_IN_DATA):
            if path.name in db_files and path.parent == config.DATA_DIR:
                continue
            tar.add(path, arcname="web/" + path.relative_to(config.DATA_DIR).as_posix())
            summary["web"] += 1

        if include_dataset:
            dataset = _dataset_dir()
            for path in _walk(dataset):
                tar.add(path, arcname="captcha_dataset_api/" + path.relative_to(dataset).as_posix())
                summary["captcha_dataset_api"] += 1

        for path in _model_files():
            tar.add(path, arcname=f"model/{path.name}")
            summary["model"] += 1

        for path in _railway_files():
            tar.add(path, arcname=f"railway/{path.name}")
            summary["railway"] += 1

        manifest = {
            "old_root": str(config.REPO_ROOT),
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "files": summary,
        }
        data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    snapshot.unlink(missing_ok=True)
    snapshot.parent.rmdir()
    return summary


# --------------------------------------------------------------------------- #
# После переезда
# --------------------------------------------------------------------------- #


def _norm(path: str) -> str:
    """Путь для сравнения: прямые слэши, без хвостового, без учёта регистра (Windows)."""
    return path.replace("\\", "/").rstrip("/").lower()


def _prefix_map(old_root: str) -> list[tuple[str, Path]]:
    """Старый префикс → новый каталог. Длинные префиксы первыми: они точнее."""
    from web.services.training import LIVE_MODEL  # noqa: PLC0415

    root = _norm(old_root)
    pairs = [
        (f"{root}/airlines_api/captcha_dataset_api", _dataset_dir()),
        (f"{root}/web/data", config.DATA_DIR),
        (f"{root}/airlines_api", LIVE_MODEL.parent),
    ]
    return sorted(pairs, key=lambda pair: -len(pair[0]))


def _relocate(value: str | None, pairs: list[tuple[str, Path]]) -> str | None:
    if not value:
        return value
    normalized = value.replace("\\", "/")
    lowered = normalized.lower()
    for old, new in pairs:
        if lowered == old or lowered.startswith(old + "/"):
            rest = normalized[len(old):].lstrip("/")
            return str(new / rest) if rest else str(new)
    return value


def relocate_paths(db, old_root: str) -> dict[str, int]:
    """
    Переписать в базе абсолютные пути старой машины на пути новой.

    В базе их немного: история разметки капч, файлы кандидатов обучения и
    каталоги запусков. Без правки панель показывала бы `D:\\projects\\...`
    там, где файлов давно нет, и не нашла бы кандидата модели.
    """
    from web.models import CaptchaSample, Job, TrainingRun  # noqa: PLC0415

    pairs = _prefix_map(old_root)
    changed = {"captcha_samples": 0, "training_runs": 0, "jobs": 0}

    for sample in db.query(CaptchaSample).all():
        new = _relocate(sample.file_path, pairs)
        if new != sample.file_path:
            sample.file_path = new
            changed["captcha_samples"] += 1

    for run in db.query(TrainingRun).all():
        new = _relocate(run.model_path, pairs)
        if new != run.model_path:
            run.model_path = new
            changed["training_runs"] += 1

    for job in db.query(Job).all():
        new = _relocate(job.run_dir, pairs)
        if new != job.run_dir:
            job.run_dir = new
            changed["jobs"] += 1

    db.commit()
    return changed


def manifest_root(data_root: Path) -> str:
    """Корень проекта старой машины из MANIFEST.json или пустая строка."""
    path = data_root / MANIFEST_NAME
    if not path.exists():
        return ""
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("old_root") or "")
    except (OSError, ValueError):
        return ""
