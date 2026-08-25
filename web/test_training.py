"""Тесты дообучения модели капчи.

Настоящее обучение здесь не запускается — оно идёт около часа. Вместо тренера
подставляется короткий скрипт-заглушка, который печатает пару строк и кладёт
на место файл модели. Проверяется то, что нельзя увидеть глазами: не подменит
ли панель модель под работающим поиском, переживёт ли откат перезапуск и не
потеряются ли метрики по дороге.

Запуск (из корня репозитория):
    python run_tests.py web/test_training.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# --------------------------------------------------------------------------- #
# Окружение
# --------------------------------------------------------------------------- #

# Заглушка вместо тренера: печатает то же, что настоящий, и сохраняет «модель».
FAKE_TRAINER = '''
import argparse, json, pathlib, sys

ap = argparse.ArgumentParser()
ap.add_argument("--epochs", type=int, default=1)
ap.add_argument("--out", type=str, required=True)
ap.add_argument("--from", dest="init_from", type=str, default="")
ap.add_argument("--resume", action="store_true")
args = ap.parse_args()

accuracy = float(pathlib.Path(__file__).with_name("accuracy.txt").read_text())
fail = pathlib.Path(__file__).with_name("fail.flag").exists()
silent = pathlib.Path(__file__).with_name("silent.flag").exists()

print(f"[train-api] fine-tune slice из {pathlib.Path(args.init_from).name}")
print(f"[train-api] epoch 1/{args.epochs} loss=0.3/0.2")
if fail:
    print("[train-api] Traceback: всё пропало", file=sys.stderr)
    raise SystemExit(1)

out = pathlib.Path(args.out)
if not silent:
    out.write_bytes(b"fake-model")
    out.with_suffix(".json").write_text(
        json.dumps({"accuracy_percent": accuracy, "samples": 100, "trained_at": "2026-08-20T12:00:00"}),
        encoding="utf-8",
    )
print("[train-api] готово.")
'''


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """База во временной папке и подменённый тренер."""
    import web.config as config
    import web.crypto as crypto
    import web.db as db_module
    import web.services.training as training

    monkeypatch.setenv("TICKET_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(crypto, "SECRET_KEY_FILE", tmp_path / "secret.key")
    monkeypatch.setattr(crypto, "SESSION_KEY_FILE", tmp_path / "session.key")
    monkeypatch.delenv("TICKET_SECRET_KEY", raising=False)
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_SessionFactory", None)

    # Ни настоящего тренера, ни настоящей модели: тесты не должны трогать
    # рабочий captcha_model_api.pt — он один на всю машину.
    trainer = tmp_path / "fake_trainer.py"
    trainer.write_text(FAKE_TRAINER, encoding="utf-8")
    (tmp_path / "accuracy.txt").write_text("90.0", encoding="utf-8")

    monkeypatch.setattr(training, "TRAINER", trainer)
    monkeypatch.setattr(training, "LIVE_MODEL", tmp_path / "model.pt")
    monkeypatch.setattr(training, "CANDIDATE_MODEL", tmp_path / "model.candidate.pt")
    monkeypatch.setattr(training, "PREVIOUS_MODEL", tmp_path / "model.prev.pt")
    monkeypatch.setattr(training, "MIN_SAMPLES", 2)
    # Датасет не читаем с диска: он общий и меняется, пока идёт поиск.
    monkeypatch.setattr(training, "_dataset_counts", lambda: (100, 4))

    db_module.init_db()
    with db_module.session_scope() as session:
        yield session


def _set_live_model(training, *, accuracy: float | None) -> None:
    training.LIVE_MODEL.write_bytes(b"live-model")
    metrics = training.LIVE_MODEL.with_suffix(".json")
    if accuracy is None:
        metrics.unlink(missing_ok=True)
    else:
        metrics.write_text(
            json.dumps({"accuracy_percent": accuracy, "samples": 50}), encoding="utf-8"
        )


def _run_to_completion(training, db, **kwargs):
    """Запустить обучение и дождаться конца заглушки."""
    run = training.start_training(db, **kwargs)
    for _ in range(200):
        if not training.is_running():
            break
        time.sleep(0.05)
    time.sleep(0.3)  # даём потоку чтения дописать итог
    db.expire_all()
    return db.get(type(run), run.id)


# --------------------------------------------------------------------------- #
# Запуск обучения
# --------------------------------------------------------------------------- #


def test_training_writes_to_its_own_file(env) -> None:
    """
    Главное правило: обучение НЕ трогает рабочую модель.

    Иначе часовой прогон подменил бы её под работающим поиском, и бот, который
    читает модель при старте, поймал бы полуфайл.
    """
    import web.services.training as training

    _set_live_model(training, accuracy=80.0)
    before = training.LIVE_MODEL.read_bytes()

    run = _run_to_completion(training, env, epochs=1)

    assert run.status == "done"
    assert training.CANDIDATE_MODEL.exists()
    assert training.LIVE_MODEL.read_bytes() == before


def test_accuracy_and_baseline_are_recorded(env) -> None:
    import web.services.training as training

    _set_live_model(training, accuracy=80.0)
    run = _run_to_completion(training, env, epochs=1)

    assert run.accuracy == 90.0
    assert run.baseline_accuracy == 80.0


def test_worse_model_is_reported_not_hidden(env) -> None:
    """
    Модель хуже прежней не прячем и не применяем сама собой.

    Точность на маленьком датасете скачет, и решение — за человеком: иногда
    падение на процент означает, что в разметку попала пара ошибок.
    """
    import web.services.training as training

    _set_live_model(training, accuracy=95.0)
    (training.TRAINER.parent / "accuracy.txt").write_text("90.0", encoding="utf-8")

    _run_to_completion(training, env, epochs=1)
    state = training.training_state(env)

    assert state["candidate"]["worse"] is True
    assert state["candidate"]["accuracy"] == 90.0
    assert state["candidate"]["baseline"] == 95.0


def test_failed_training_leaves_no_candidate(env) -> None:
    import web.services.training as training

    _set_live_model(training, accuracy=80.0)
    (training.TRAINER.parent / "fail.flag").write_text("", encoding="utf-8")

    run = _run_to_completion(training, env, epochs=1)

    assert run.status == "failed"
    assert training.training_state(env)["candidate"] is None


def test_zero_exit_without_model_is_a_failure(env) -> None:
    """
    Процесс вышел нулём, но файла нет — считать это успехом нельзя:
    кнопка «сделать рабочей» вела бы в никуда.
    """
    import web.services.training as training

    _set_live_model(training, accuracy=80.0)
    (training.TRAINER.parent / "silent.flag").write_text("", encoding="utf-8")

    run = _run_to_completion(training, env, epochs=1)
    assert run.status == "failed"


def test_too_few_samples_is_refused(env) -> None:
    import web.services.training as training

    training.MIN_SAMPLES = 500
    try:
        with pytest.raises(training.TrainingError):
            training.start_training(env, epochs=1)
    finally:
        training.MIN_SAMPLES = 2


def test_log_reaches_the_database(env) -> None:
    import web.services.training as training
    from web.models import TrainingLog

    _set_live_model(training, accuracy=80.0)
    run = _run_to_completion(training, env, epochs=1)

    texts = [
        row.text
        for row in env.query(TrainingLog).filter(TrainingLog.run_id == run.id).all()
    ]
    assert any("fine-tune" in text for text in texts)
    assert any("готово" in text for text in texts)


def test_progress_is_read_from_the_log(env) -> None:
    """Ждать час без единой цифры на экране невыносимо — показываем эпохи."""
    import web.services.training as training

    _set_live_model(training, accuracy=80.0)
    run = _run_to_completion(training, env, epochs=7)

    assert training.epoch_progress(env, run) == (1, 7)


# --------------------------------------------------------------------------- #
# Подмена рабочей модели
# --------------------------------------------------------------------------- #


def _running_airlines_job(db):
    from web.models import Job, JobStatus, Role, Service, User
    from web.security import hash_password

    user = db.query(User).first()
    if user is None:
        user = User(login="tester", password_hash=hash_password("x" * 8), role=Role.USER.value)
        db.add(user)
        db.flush()

    job = Job(
        service=Service.AIRLINES.value,
        user_id=user.id,
        source="ASB",
        destination="KRW",
        depart_date="10.08.2026",
        status=JobStatus.RUNNING.value,
    )
    db.add(job)
    db.commit()
    return job


def test_apply_is_refused_while_airlines_searches(env) -> None:
    """
    Бот читает модель при запуске. Подменять её под работающим поиском
    незачем: либо ничего не изменится, либо поймаем его ровно на чтении.
    """
    import web.services.training as training

    _set_live_model(training, accuracy=80.0)
    run = _run_to_completion(training, env, epochs=1)
    _running_airlines_job(env)

    with pytest.raises(training.TrainingError) as exc:
        training.apply_model(env, run)
    assert "поиск" in str(exc.value).lower()


def test_apply_swaps_model_and_metrics(env) -> None:
    import web.services.training as training

    _set_live_model(training, accuracy=80.0)
    run = _run_to_completion(training, env, epochs=1)

    training.apply_model(env, run)

    assert training.LIVE_MODEL.read_bytes() == b"fake-model"
    # Метрики обязаны ехать вместе с моделью, иначе панель покажет чужие цифры.
    assert training.current_model()["accuracy_percent"] == 90.0
    assert run.applied_at is not None


def test_previous_model_is_kept_for_rollback(env) -> None:
    import web.services.training as training

    _set_live_model(training, accuracy=80.0)
    run = _run_to_completion(training, env, epochs=1)
    training.apply_model(env, run)

    assert training.PREVIOUS_MODEL.read_bytes() == b"live-model"
    assert training.current_model()["has_previous"] is True


def test_rollback_restores_the_previous_model(env) -> None:
    import web.services.training as training

    _set_live_model(training, accuracy=80.0)
    run = _run_to_completion(training, env, epochs=1)
    training.apply_model(env, run)

    training.rollback_model(env)

    assert training.LIVE_MODEL.read_bytes() == b"live-model"
    assert training.current_model()["accuracy_percent"] == 80.0


def test_rollback_can_be_undone(env) -> None:
    """Откат меняет модели местами: передумать должно быть так же дёшево."""
    import web.services.training as training

    _set_live_model(training, accuracy=80.0)
    run = _run_to_completion(training, env, epochs=1)
    training.apply_model(env, run)
    training.rollback_model(env)

    training.rollback_model(env)
    assert training.LIVE_MODEL.read_bytes() == b"fake-model"
    assert training.current_model()["accuracy_percent"] == 90.0


def test_rollback_without_previous_is_refused(env) -> None:
    import web.services.training as training

    _set_live_model(training, accuracy=80.0)
    with pytest.raises(training.TrainingError):
        training.rollback_model(env)


def test_applied_model_is_not_offered_again(env) -> None:
    import web.services.training as training

    _set_live_model(training, accuracy=80.0)
    run = _run_to_completion(training, env, epochs=1)
    training.apply_model(env, run)

    assert training.training_state(env)["candidate"] is None
    with pytest.raises(training.TrainingError):
        training.apply_model(env, run)


# --------------------------------------------------------------------------- #
# Перезапуск панели
# --------------------------------------------------------------------------- #


def test_orphan_run_is_marked_stopped(env) -> None:
    """
    Час обучения не переживает перезапуск панели, а запись «идёт обучение»
    осталась бы висеть и не дала бы запустить новое.
    """
    import web.services.training as training
    from web.models import TrainingRun, TrainingStatus

    env.add(TrainingRun(status=TrainingStatus.RUNNING.value, samples=100))
    env.commit()

    assert training.recover_orphans(env) == 1
    assert training.latest_run(env).status == TrainingStatus.STOPPED.value


def test_state_counts_new_samples(env) -> None:
    import web.services.training as training

    _set_live_model(training, accuracy=80.0)  # в метриках samples=50
    state = training.training_state(env)

    # Всего 104 (100 confirmed + 4 hard), в обучении было 50.
    assert state["samples_total"] == 104
    assert state["samples_new"] == 54


def test_unknown_baseline_gives_no_new_count(env) -> None:
    """
    У модели без метрик точки отсчёта нет. Написать «новых 104» значило бы
    соврать: эти капчи в обучении, скорее всего, уже были.
    """
    import web.services.training as training

    _set_live_model(training, accuracy=None)
    assert training.training_state(env)["samples_new"] is None
