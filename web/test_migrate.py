"""Тесты переезда: архив данных и правка путей старой машины.

Запуск (из корня репозитория):
    python run_tests.py web/test_migrate.py
"""

from __future__ import annotations

import json
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "airlines_api")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

OLD_ROOT = "D:\\projects\\ticket"


@pytest.fixture()
def place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Рабочая раскладка во временной папке: данные, набор капч, модель, поезд."""
    import captcha_dataset_api
    import web.config as config
    import web.crypto as crypto
    import web.db as db_module
    import web.services.training as training

    data = tmp_path / "web"
    dataset = tmp_path / "captcha_dataset_api"
    model = tmp_path / "model" / "captcha_model_api.pt"
    railway = tmp_path / "railway"
    for folder in (data, dataset / "hard", model.parent, railway, data / "runs" / "job-1",
                   data / "providers" / "1"):
        folder.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("TICKET_DB_PATH", str(data / "ticket.db"))
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "RAILWAY_DATA_DIR", railway)
    monkeypatch.setattr(crypto, "SECRET_KEY_FILE", data / "secret.key")
    monkeypatch.setattr(crypto, "SESSION_KEY_FILE", data / "session.key")
    monkeypatch.delenv("TICKET_SECRET_KEY", raising=False)
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_SessionFactory", None)
    monkeypatch.setattr(captcha_dataset_api, "DATASET_DIR", dataset)
    monkeypatch.setattr(training, "LIVE_MODEL", model)

    db_module.init_db()
    # Ключ шифрования появляется при первом сохранённом пароле; у рабочей панели он есть.
    (data / "secret.key").write_bytes(b"key")
    (data / "runs" / "job-1" / "check.env").write_text("API_PASSWORD=secret\n", encoding="utf-8")
    (data / "providers" / "1" / ".env.local").write_text("TOKEN=x\n", encoding="utf-8")
    (dataset / "hard" / "12345_ab.png").write_bytes(b"png")
    model.write_bytes(b"weights")
    model.with_name("captcha_model_api.prev.pt").write_bytes(b"old")
    model.with_name("captcha_model_api.candidate.pt").write_bytes(b"draft")
    (railway / "railway_bookings.json").write_text("{}", encoding="utf-8")
    return {"data": data, "dataset": dataset, "model": model, "railway": railway,
            "db": db_module}


def _names(archive: Path) -> set[str]:
    with tarfile.open(archive) as tar:
        return set(tar.getnames())


def test_archive_has_the_volume_layout(place, tmp_path: Path) -> None:
    from web.migrate import export_data

    archive = tmp_path / "out" / "data.tar.gz"
    export_data(archive)
    names = _names(archive)

    for expected in ("web/ticket.db", "web/secret.key", "web/providers/1/.env.local",
                     "captcha_dataset_api/hard/12345_ab.png", "model/captcha_model_api.pt",
                     "model/captcha_model_api.prev.pt", "railway/railway_bookings.json",
                     "MANIFEST.json"):
        assert expected in names, expected


def test_archive_skips_run_files_with_passwords_and_drafts(place, tmp_path: Path) -> None:
    """check.env с паролями пересоздаётся при запуске — в архиве ему делать нечего."""
    from web.migrate import export_data

    archive = tmp_path / "data.tar.gz"
    export_data(archive)
    names = _names(archive)

    assert not any(name.startswith("web/runs") for name in names)
    assert "model/captcha_model_api.candidate.pt" not in names
    assert not any(name.endswith(("-wal", "-shm")) for name in names)


def test_database_copy_keeps_latest_rows(place, tmp_path: Path) -> None:
    """База в WAL: копия файлом теряет последние записи, backup — нет."""
    from web.migrate import export_data
    from web.models import Setting

    with place["db"].session_scope() as session:
        session.add(Setting(key="probe", value="последняя запись"))

    archive = tmp_path / "data.tar.gz"
    export_data(archive)
    out = tmp_path / "unpacked"
    with tarfile.open(archive) as tar:
        tar.extract("web/ticket.db", out)

    import sqlite3

    con = sqlite3.connect(out / "web" / "ticket.db")
    try:
        assert con.execute("select value from settings where key='probe'").fetchone() == (
            "последняя запись",
        )
    finally:
        con.close()


def test_manifest_remembers_old_root(place, tmp_path: Path) -> None:
    from web import config
    from web.migrate import export_data

    archive = tmp_path / "data.tar.gz"
    export_data(archive)
    with tarfile.open(archive) as tar:
        manifest = json.loads(tar.extractfile("MANIFEST.json").read())
    assert manifest["old_root"] == str(config.REPO_ROOT)


def test_dataset_can_be_left_out(place, tmp_path: Path) -> None:
    from web.migrate import export_data

    archive = tmp_path / "data.tar.gz"
    export_data(archive, include_dataset=False)
    assert not any(name.startswith("captcha_dataset_api/") for name in _names(archive))


def test_windows_paths_are_moved_to_the_new_place(place) -> None:
    from web.migrate import relocate_paths
    from web.models import CaptchaSample, Job, TrainingRun, User

    with place["db"].session_scope() as session:
        user = User(login="u", password_hash="x", role="user")
        session.add(user)
        session.flush()
        session.add(CaptchaSample(
            service="airlines", status="labeled",
            file_path=f"{OLD_ROOT}\\airlines_api\\captcha_dataset_api\\hard\\19660_783e.png"))
        session.add(TrainingRun(
            status="finished",
            model_path=f"{OLD_ROOT}\\airlines_api\\captcha_model_api.candidate.pt"))
        session.add(Job(user_id=user.id, service="airlines", source="ASB", destination="MYP",
                        depart_date="01.01.2027", run_dir=f"{OLD_ROOT}\\web\\data\\runs\\job-2"))
        session.add(CaptchaSample(service="airlines", status="labeled",
                                  file_path="/elsewhere/keep.png"))

    with place["db"].session_scope() as session:
        changed = relocate_paths(session, OLD_ROOT)

    with place["db"].session_scope() as session:
        paths = [s.file_path for s in session.query(CaptchaSample).order_by(CaptchaSample.id)]
        run = session.query(TrainingRun).one()
        job = session.query(Job).one()

    assert Path(paths[0]) == place["dataset"] / "hard" / "19660_783e.png"
    assert paths[1] == "/elsewhere/keep.png", "чужой путь не трогаем"
    assert Path(run.model_path) == place["model"].parent / "captcha_model_api.candidate.pt"
    assert Path(job.run_dir) == place["data"] / "runs" / "job-2"
    assert changed == {"captcha_samples": 1, "training_runs": 1, "jobs": 1}


def test_relocate_runs_once(place) -> None:
    """Перезапуск контейнера не должен переписывать пути заново."""
    from web import config
    from web.cli import main
    from web.migrate import MANIFEST_NAME, RELOCATED_MARK

    (config.DATA_DIR.parent / MANIFEST_NAME).write_text(
        json.dumps({"old_root": OLD_ROOT}), encoding="utf-8")

    assert main(["relocate-paths", "--if-needed"]) == 0
    mark = config.DATA_DIR / RELOCATED_MARK
    assert mark.exists()

    mark.write_text("уже было", encoding="utf-8")
    assert main(["relocate-paths", "--if-needed"]) == 0
    assert mark.read_text(encoding="utf-8") == "уже было"


def test_relocate_without_manifest_is_a_no_op(place) -> None:
    from web import config
    from web.cli import main
    from web.migrate import RELOCATED_MARK

    assert main(["relocate-paths", "--if-needed"]) == 0
    assert not (config.DATA_DIR / RELOCATED_MARK).exists()
