"""Дообучение модели капчи: запуск, живой лог, подмена рабочей модели.

Устройство такое же скучное, как у поиска: отдельный процесс, построчное
чтение stdout в поток, статус по коду выхода. Разница в двух правилах.

**Обучение не занимает очередь сервиса.** На процессоре сотня эпох — это около
часа. Останавливать на час поиск ради модели бессмысленно: билет важнее.
Поэтому обучение спокойно идёт параллельно с поиском самолётов.

**Зато подмена рабочей модели ждёт.** Обучение всегда пишет в свой файл
(`--out`), а живую `captcha_model_api.pt` заменяет отдельное действие, и вот
оно уже требует, чтобы самолётный поиск был остановлен: бот читает модель
в момент старта, и подменять её под работающим процессом незачем.

Предыдущая модель не удаляется, а отъезжает в `.prev`. Точность на маленьком
датасете скачет, и «откатиться» должно быть одной кнопкой, а не поиском бэкапа.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

from web.config import AIRLINES_API_DIR, REPO_ROOT
from web.db import session_scope
from web.models import (
    JobStatus,
    LogLevel,
    Service,
    TrainingLog,
    TrainingRun,
    TrainingStatus,
)

__all__ = [
    "TrainingError",
    "start_training",
    "stop_training",
    "apply_model",
    "rollback_model",
    "is_running",
    "active_run",
    "latest_run",
    "current_model",
    "training_state",
    "recover_orphans",
    "stop_all",
]

TRAINER = AIRLINES_API_DIR / "train_captcha_cnn_api.py"
LIVE_MODEL = AIRLINES_API_DIR / "captcha_model_api.pt"
CANDIDATE_MODEL = AIRLINES_API_DIR / "captcha_model_api.candidate.pt"
PREVIOUS_MODEL = AIRLINES_API_DIR / "captcha_model_api.prev.pt"

# Обучение печатает десяток строк на сотню эпох — предел нужен только на случай
# бесконечного потока ошибок из torch.
MAX_LOGS_PER_RUN = 5_000

# Ниже этого числа размеченных капч обучать нет смысла: модель переобучится
# на десятке картинок и станет хуже той, что есть.
MIN_SAMPLES = 20

_process: subprocess.Popen | None = None
_lock = threading.Lock()


class TrainingError(RuntimeError):
    """Обучение нельзя запустить или применить — с объяснением для человека."""


# --------------------------------------------------------------------------- #
# Состояние
# --------------------------------------------------------------------------- #


def is_running() -> bool:
    with _lock:
        process = _process
    return process is not None and process.poll() is None


def active_run(db) -> TrainingRun | None:
    return (
        db.query(TrainingRun)
        .filter(TrainingRun.status == TrainingStatus.RUNNING.value)
        .order_by(TrainingRun.id.desc())
        .first()
    )


def latest_run(db) -> TrainingRun | None:
    return db.query(TrainingRun).order_by(TrainingRun.id.desc()).first()


def _cnn():
    """
    Модуль бота с моделью. Импорт ленивый: он тянет torch, а это секунды
    на старте панели ради функции, которая нужна только на странице капч.
    """
    if str(AIRLINES_API_DIR) not in sys.path:
        sys.path.insert(0, str(AIRLINES_API_DIR))
    import captcha_cnn_api  # noqa: PLC0415

    return captcha_cnn_api


def current_model() -> dict:
    """
    Что известно про рабочую модель: когда обучена, на скольких образцах, точность.

    Пустые поля — нормальный ответ: модель могли обучить до появления метрик,
    и панель обязана показать «неизвестно», а не упасть.
    """
    info = {
        "exists": LIVE_MODEL.exists(),
        "size_kb": round(LIVE_MODEL.stat().st_size / 1024) if LIVE_MODEL.exists() else 0,
        "has_previous": PREVIOUS_MODEL.exists(),
    }
    try:
        info.update(_cnn().read_metrics(LIVE_MODEL))
    except Exception:  # noqa: BLE001 — torch может быть не установлен
        pass
    return info


def _candidate_metrics() -> dict:
    try:
        return _cnn().read_metrics(CANDIDATE_MODEL)
    except Exception:  # noqa: BLE001
        return {}


def _dataset_counts() -> tuple[int, int]:
    """(confirmed, hard) — сколько размеченного лежит в датасете прямо сейчас."""
    from web.services.captcha import stats  # noqa: PLC0415

    data = stats()
    return int(data.get("confirmed", 0)), int(data.get("hard", 0))


def training_state(db) -> dict:
    """Всё, что нужно странице капч, одним словарём."""
    confirmed, hard = _dataset_counts()
    total = confirmed + hard
    model = current_model()
    run = latest_run(db)

    # Сколько прибавилось с прошлого обучения. None, а не 0 и не total:
    # у модели, обученной до появления метрик, точки отсчёта нет, и написать
    # «новых 1280» значило бы соврать — эти капчи в обучении, скорее всего, уже были.
    trained_on = model.get("samples")
    fresh = max(0, total - int(trained_on)) if trained_on else None

    candidate = None
    if run is not None and run.status == TrainingStatus.DONE.value and not run.applied_at:
        if Path(run.model_path or CANDIDATE_MODEL).exists():
            candidate = {
                "run_id": run.id,
                "accuracy": run.accuracy,
                "baseline": run.baseline_accuracy,
                "samples": run.samples,
                "finished_at": run.finished_at,
                # Точность на маленьком датасете скачет: показываем оба числа
                # и решение оставляем человеку, а не прячем модель молча.
                "worse": (
                    run.accuracy is not None
                    and run.baseline_accuracy is not None
                    and run.accuracy < run.baseline_accuracy
                ),
            }

    return {
        "model": model,
        "samples_total": total,
        "samples_new": fresh,
        "min_samples": MIN_SAMPLES,
        "running": is_running(),
        "run": run,
        "candidate": candidate,
        "can_start": total >= MIN_SAMPLES and not is_running(),
        "blocked_reason": _start_blocker(db, total),
    }


def _start_blocker(db, total: int) -> str:
    """Почему кнопку «обучить» нажимать нельзя. Пустая строка — можно."""
    if is_running():
        return "Обучение уже идёт"
    if not TRAINER.exists():
        return f"Не найден {TRAINER.name}"
    if total < MIN_SAMPLES:
        return (
            f"Размечено {total} капч из {MIN_SAMPLES} нужных: "
            f"на меньшем модель переобучится и станет хуже"
        )
    return ""


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #


def _log(db, run_id: int, text: str, level: str = LogLevel.INFO.value) -> None:
    db.add(TrainingLog(run_id=run_id, text=text, level=level))
    db.commit()


def _popen_kwargs() -> dict:
    """Отдельная группа процессов — чтобы остановка не задела саму панель."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def start_training(db, *, user_id: int | None = None, epochs: int = 100) -> TrainingRun:
    """Запустить дообучение. Бросает `TrainingError`, если запускать нельзя."""
    global _process

    confirmed, hard = _dataset_counts()
    total = confirmed + hard

    blocker = _start_blocker(db, total)
    if blocker:
        raise TrainingError(blocker)

    baseline = current_model().get("accuracy_percent")

    run = TrainingRun(
        status=TrainingStatus.RUNNING.value,
        started_by_id=user_id,
        samples=total,
        epochs=max(1, min(1000, epochs)),
        model_path=str(CANDIDATE_MODEL),
        baseline_accuracy=float(baseline) if baseline is not None else None,
    )
    db.add(run)
    db.commit()

    env = os.environ.copy()
    # Обучение печатает по-русски; без этого на Windows вывод приходит в cp1251
    # и превращается в мусор ещё до того, как мы его прочитаем.
    env["PYTHONIOENCODING"] = "utf-8"
    # Иначе строки копятся в буфере и лог в панели отстаёт на минуты.
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_COLOR"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT)

    args = [
        sys.executable,
        str(TRAINER),
        "--epochs",
        str(run.epochs),
        # Всегда в отдельный файл: рабочую модель читает бот, и подменять её
        # из-под работающего поиска нельзя. Подмена — отдельное действие.
        "--out",
        str(CANDIDATE_MODEL),
        # Дообучение с текущих весов, а не с нуля: на нашем размере датасета
        # обучение с нуля каждый раз теряет всё, что модель уже умела.
        "--resume",
        # Источник весов — рабочая модель, а не файл из --out. Без этого
        # первый же запуск не нашёл бы там ничего и начал с чистого листа.
        "--from",
        str(LIVE_MODEL),
    ]

    try:
        process = subprocess.Popen(
            args,
            cwd=str(AIRLINES_API_DIR),
            env=env,
            stdout=subprocess.PIPE,
            # Ошибки torch должны попасть в тот же лог: разбирать два потока
            # порознь при разборе неудачи невозможно.
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **_popen_kwargs(),
        )
    except OSError as exc:
        run.status = TrainingStatus.FAILED.value
        run.finished_at = dt.datetime.now()
        db.commit()
        raise TrainingError(f"не удалось запустить обучение: {exc}") from exc

    run.pid = process.pid
    db.commit()

    with _lock:
        _process = process

    _log(db, run.id, f"Запущено обучение, процесс {process.pid}, эпох: {run.epochs}")
    _log(
        db,
        run.id,
        f"Датасет: {confirmed} подтверждённых + {hard} размеченных вручную",
    )
    _log(
        db,
        run.id,
        "На процессоре это примерно 40 секунд на эпоху. Поиск не останавливается: "
        "результат ляжет в отдельный файл, подменить модель нужно будет отдельно.",
    )

    reader = threading.Thread(
        target=_pump_output,
        args=(run.id, process),
        name=f"training-{run.id}-output",
        daemon=True,
    )
    reader.start()
    return run


# --------------------------------------------------------------------------- #
# Чтение вывода
# --------------------------------------------------------------------------- #

_ERROR_MARKERS = ("traceback", "error", "ошибка", "не найден", "exception")
_OK_MARKERS = ("готово", "сохранена")


def classify(line: str) -> str:
    low = line.lower()
    if any(marker in low for marker in _ERROR_MARKERS):
        return LogLevel.ERROR.value
    if any(marker in low for marker in _OK_MARKERS):
        return LogLevel.OK.value
    if low.startswith("[warn]") or "предупрежд" in low:
        return LogLevel.WARN.value
    return LogLevel.INFO.value


def _pump_output(run_id: int, process: subprocess.Popen) -> None:
    """Построчно перекладывать вывод обучения в базу. Отдельный поток."""
    written = 0
    try:
        for raw in process.stdout:  # type: ignore[union-attr]
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            if written >= MAX_LOGS_PER_RUN:
                # Не обрываем чтение: процесс должен доработать.
                continue
            try:
                with session_scope() as db:
                    db.add(
                        TrainingLog(run_id=run_id, text=line[:4000], level=classify(line))
                    )
                written += 1
            except Exception as exc:  # noqa: BLE001 — база не должна ронять чтение
                print(f"[training] run {run_id}: не записал строку лога: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"[training] run {run_id}: чтение вывода прервано: {exc}")
    finally:
        code = process.wait()
        _finish(run_id, code)


def _finish(run_id: int, code: int) -> None:
    """Проставить итог обучения и подобрать метрики модели-кандидата."""
    global _process

    with _lock:
        _process = None

    try:
        with session_scope() as db:
            run = db.get(TrainingRun, run_id)
            if run is None:
                return

            stopped_by_human = run.status == TrainingStatus.STOPPED.value
            metrics = _candidate_metrics()

            if stopped_by_human:
                message = f"Остановлено по команде (код выхода {code})"
                level = LogLevel.WARN.value
            elif code == 0 and metrics:
                run.status = TrainingStatus.DONE.value
                run.accuracy = metrics.get("accuracy_percent")
                run.samples = int(metrics.get("samples") or run.samples)
                level = LogLevel.OK.value
                message = _verdict(run)
            elif code == 0:
                # Процесс вышел нулём, но метрик нет — значит модель не сохранилась.
                # Считать это успехом нельзя: подменять будет нечем.
                run.status = TrainingStatus.FAILED.value
                level = LogLevel.ERROR.value
                message = "Обучение завершилось, но файл модели не появился"
            else:
                run.status = TrainingStatus.FAILED.value
                level = LogLevel.ERROR.value
                message = f"Обучение завершилось с кодом {code}"

            run.exit_code = code
            run.pid = None
            run.finished_at = dt.datetime.now()
            db.add(TrainingLog(run_id=run_id, level=level, text=message))
            db.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"[training] run {run_id}: не записал итог: {exc}")


def _verdict(run: TrainingRun) -> str:
    """Человеческий итог: стало лучше, хуже или не с чем сравнить."""
    if run.accuracy is None:
        return "Модель обучена. Точность неизвестна — сравнить не с чем."
    if run.baseline_accuracy is None:
        return (
            f"Модель обучена: {run.accuracy:.1f}% на {run.samples} капчах. "
            f"У прежней модели метрик нет, сравнить не с чем."
        )
    delta = run.accuracy - run.baseline_accuracy
    if delta > 0:
        return (
            f"Модель обучена: {run.accuracy:.1f}% против {run.baseline_accuracy:.1f}% "
            f"у рабочей (+{delta:.1f}). Можно применять."
        )
    if delta < 0:
        return (
            f"Модель обучена: {run.accuracy:.1f}% против {run.baseline_accuracy:.1f}% "
            f"у рабочей ({delta:.1f}). Стало хуже — применять не стоит."
        )
    return f"Модель обучена: {run.accuracy:.1f}% — как у рабочей."


# --------------------------------------------------------------------------- #
# Остановка
# --------------------------------------------------------------------------- #


def stop_training(db, run: TrainingRun, *, timeout: float = 15.0) -> bool:
    """Остановить обучение. True — остановлено, False — процесса и не было."""
    with _lock:
        process = _process

    if process is None or process.poll() is not None:
        # Процесса нет: панель перезапускали, а запись осталась висеть.
        if run.status == TrainingStatus.RUNNING.value:
            run.status = TrainingStatus.STOPPED.value
            run.pid = None
            run.finished_at = dt.datetime.now()
            db.commit()
        return False

    run.status = TrainingStatus.STOPPED.value
    db.commit()
    _log(db, run.id, "Остановка по команде человека", LogLevel.WARN.value)

    try:
        if sys.platform == "win32":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
    except (OSError, ValueError) as exc:
        print(f"[training] run {run.id}: сигнал не доставлен ({exc}) — убиваю")
        process.kill()
        return True

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _log(
            db,
            run.id,
            f"Не завершилось за {timeout:.0f}с — принудительная остановка",
            LogLevel.WARN.value,
        )
        process.kill()

    return True


def recover_orphans(db) -> int:
    """Починить записи, оставшиеся «в работе» после падения панели."""
    if is_running():
        return 0
    stale = (
        db.query(TrainingRun)
        .filter(TrainingRun.status == TrainingStatus.RUNNING.value)
        .all()
    )
    for run in stale:
        run.status = TrainingStatus.STOPPED.value
        run.pid = None
        run.finished_at = dt.datetime.now()
        db.add(
            TrainingLog(
                run_id=run.id,
                level=LogLevel.WARN.value,
                text="Панель перезапускалась — обучение не пережило перезапуск",
            )
        )
    if stale:
        db.commit()
    return len(stale)


def stop_all() -> None:
    """Убить процесс обучения при остановке панели."""
    global _process

    with _lock:
        process = _process
        _process = None
    if process is not None and process.poll() is None:
        process.kill()


# --------------------------------------------------------------------------- #
# Подмена рабочей модели
# --------------------------------------------------------------------------- #


def _airlines_search_running(db) -> bool:
    """Идёт ли сейчас поиск самолётов — тот, кто читает модель."""
    from web.models import Job  # noqa: PLC0415

    return (
        db.query(Job)
        .filter(
            Job.service == Service.AIRLINES.value,
            Job.status.in_((JobStatus.RUNNING.value, JobStatus.QUEUED.value)),
        )
        .first()
        is not None
    )


def apply_model(db, run: TrainingRun) -> str:
    """
    Сделать обученную модель рабочей. Возвращает сообщение для человека.

    Ждём остановки поиска самолётов: бот читает модель при старте и кэширует
    её, так что подмена под работающим процессом либо ничего не изменит,
    либо поймает его ровно на чтении. Ни то ни другое не нужно.
    """
    candidate = Path(run.model_path or CANDIDATE_MODEL)
    if not candidate.exists():
        raise TrainingError(f"Файл обученной модели не найден: {candidate.name}")
    if run.status != TrainingStatus.DONE.value:
        raise TrainingError("Это обучение не завершилось успешно")
    if run.applied_at:
        raise TrainingError("Эта модель уже применена")
    if _airlines_search_running(db):
        raise TrainingError(
            "Идёт поиск по самолётам. Бот читает модель при запуске — "
            "остановите поиск, примените модель и запустите заново."
        )

    if LIVE_MODEL.exists():
        # Прежняя модель не удаляется: точность на маленьком датасете скачет,
        # и откат должен быть одной кнопкой, а не поиском бэкапа.
        shutil.copy2(LIVE_MODEL, PREVIOUS_MODEL)
        _copy_metrics(LIVE_MODEL, PREVIOUS_MODEL)

    shutil.copy2(candidate, LIVE_MODEL)
    _copy_metrics(candidate, LIVE_MODEL)

    run.applied_at = dt.datetime.now()
    db.commit()
    _log(db, run.id, "Модель применена — стала рабочей", LogLevel.OK.value)

    accuracy = f"{run.accuracy:.1f}%" if run.accuracy is not None else "неизвестна"
    return f"Модель применена (точность {accuracy}). Прежняя сохранена для отката."


def rollback_model(db) -> str:
    """Вернуть предыдущую модель."""
    if not PREVIOUS_MODEL.exists():
        raise TrainingError("Предыдущей модели нет — откатывать не к чему")
    if _airlines_search_running(db):
        raise TrainingError(
            "Идёт поиск по самолётам. Остановите его, откатите модель "
            "и запустите заново."
        )

    # Меняем местами, а не затираем: откат должен откатываться обратно.
    swap = LIVE_MODEL.with_name(LIVE_MODEL.name + ".swap")
    if LIVE_MODEL.exists():
        shutil.copy2(LIVE_MODEL, swap)
        _copy_metrics(LIVE_MODEL, swap)

    shutil.copy2(PREVIOUS_MODEL, LIVE_MODEL)
    _copy_metrics(PREVIOUS_MODEL, LIVE_MODEL)

    if swap.exists():
        shutil.move(str(swap), str(PREVIOUS_MODEL))
        _copy_metrics(swap, PREVIOUS_MODEL, move=True)
    else:
        PREVIOUS_MODEL.unlink(missing_ok=True)

    return "Откатились на предыдущую модель. Запустите поиск заново."


def _copy_metrics(source: Path, target: Path, *, move: bool = False) -> None:
    """Метрики ходят вместе с моделью, иначе панель покажет чужие цифры."""
    src = source.with_suffix(".json")
    dst = target.with_suffix(".json")
    if not src.exists():
        dst.unlink(missing_ok=True)
        return
    if move:
        shutil.move(str(src), str(dst))
    else:
        shutil.copy2(src, dst)


def format_duration(run: TrainingRun) -> str:
    """Сколько шло обучение — словами."""
    end = run.finished_at or dt.datetime.now()
    seconds = int((end - run.started_at).total_seconds())
    if seconds < 60:
        return f"{seconds} с"
    return f"{seconds // 60} мин {seconds % 60:02d} с"


def epoch_progress(db, run: TrainingRun) -> tuple[int, int]:
    """
    Где сейчас обучение: (сделано эпох, всего). Читается из последней строки лога.

    Отдельного канала прогресса у тренера нет, а ждать час без единой цифры
    на экране невыносимо. Формат строки: `[train-api] epoch 40/100 ...`.
    """
    row = (
        db.query(TrainingLog)
        .filter(TrainingLog.run_id == run.id, TrainingLog.text.like("%epoch %"))
        .order_by(TrainingLog.id.desc())
        .first()
    )
    if row is None:
        return 0, run.epochs
    found = re.search(r"epoch (\d+)/(\d+)", row.text)
    if not found:
        return 0, run.epochs
    return int(found.group(1)), int(found.group(2))
