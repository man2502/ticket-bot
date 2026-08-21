"""Запуск ботов отдельными процессами и чтение их вывода.

Устройство простое и намеренно скучное:

    панель → готовит check.env и passengers.json → запускает bot.py --env ...
           → построчно читает stdout в отдельном потоке → пишет в job_logs
           → по коду выхода ставит статус процесса

Почему отдельный процесс, а не поток внутри панели:

* падение бота не роняет сайт;
* остановить можно гарантированно, сигналом, а не флажком, который поток
  проверит когда-нибудь;
* у бота внутри `time.sleep`, блокирующие HTTP и `os.execv` — в потоке
  веб-приложения этому не место.

**Один активный процесс на сервис.** Два параллельных поиска с одного адреса
в одну и ту же секунду — это не вдвое больше шансов, а бан. Второй запуск
встаёт в очередь и стартует, когда освободится место.

Коды выхода бота:

    0   бронь создана или места найдены — успех
    2   бронь под вопросом: нажали «подтвердить», ответа нет; нужен человек
    1   ошибка
    130 остановлен человеком (KeyboardInterrupt)
"""

from __future__ import annotations

import datetime as dt
import os
import signal
import subprocess
import sys
import threading

from web.config import REPO_ROOT
from web.db import session_scope
from web.models import Job, JobLog, JobStatus, LogLevel
from web.services.bot_env import local_env_warning, prepare_launch

__all__ = [
    "start_job",
    "stop_job",
    "is_running",
    "active_job_id",
    "recover_orphans",
    "JobStartError",
]

# Живые процессы этого запуска панели: job_id → Popen.
_processes: dict[int, subprocess.Popen] = {}
_lock = threading.Lock()

# Сколько строк лога хранить на процесс. Поиск может идти сутками по два
# запроса в минуту — без предела база растёт бесконечно.
MAX_LOGS_PER_JOB = 20_000


class JobStartError(RuntimeError):
    """Процесс нельзя запустить: занята очередь, нет данных, нет доступов."""


# --------------------------------------------------------------------------- #
# Разбор строк вывода
# --------------------------------------------------------------------------- #

# Боты печатают строки вида «[ok] ...», «[--] ...», «[error] ...».
# Уровень берём из тега — менять сами боты ради этого не потребовалось.
_ERROR_TAGS = ("[error]", "[auth]", "[fatal]")
_WARN_TAGS = ("[warn]", "[rate-limit]", "[backoff]", "[maintenance]", "[compare] ✗")
_OK_TAGS = ("[ok]", "[book-railway] booking=", "[book-api] pnr=")


def classify(line: str) -> str:
    """Уровень строки лога по её тегу."""
    low = line.lower()
    if any(tag in low for tag in _ERROR_TAGS):
        return LogLevel.ERROR.value
    if any(tag in low for tag in _WARN_TAGS):
        return LogLevel.WARN.value
    if any(tag in low for tag in _OK_TAGS):
        return LogLevel.OK.value
    return LogLevel.INFO.value


# --------------------------------------------------------------------------- #
# Состояние
# --------------------------------------------------------------------------- #


def is_running(job_id: int) -> bool:
    with _lock:
        process = _processes.get(job_id)
    return process is not None and process.poll() is None


def active_job_id(db, service: str) -> int | None:
    """Какой процесс сейчас занимает очередь сервиса."""
    job = (
        db.query(Job)
        .filter(Job.service == service, Job.status == JobStatus.RUNNING.value)
        .first()
    )
    return job.id if job else None


def _log(db, job_id: int, text: str, level: str = LogLevel.INFO.value) -> None:
    db.add(JobLog(job_id=job_id, text=text, level=level))
    db.commit()


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #


def _python_executable() -> str:
    """
    Интерпретатор для бота — тот же, которым запущена панель.

    В нём уже стоят зависимости ботов, и re-exec внутри `core.bootstrap`
    не сработает (он видит тот же путь) — лишнего перезапуска не будет.
    """
    return sys.executable


def _popen_kwargs() -> dict:
    """
    Аргументы Popen, различающиеся по ОС.

    Процесс кладём в отдельную группу, чтобы останавливать именно бота,
    не задев саму панель: на Windows CTRL_BREAK летит всей группе, и без
    отдельной группы панель убила бы сама себя.
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def start_job(db, job: Job) -> None:
    """
    Запустить процесс поиска. Бросает `JobStartError`, если запускать нельзя.

    Проверки очереди делаются здесь, а не в роуте: запуск возможен и из других
    мест (повтор, автозапуск), а правило «один процесс на сервис» должно
    соблюдаться всегда.
    """
    if is_running(job.id):
        raise JobStartError("Процесс уже запущен")

    busy = active_job_id(db, job.service)
    if busy is not None and busy != job.id:
        raise JobStartError(
            f"По этому сервису уже работает процесс #{busy}. "
            f"Два поиска одновременно — это бан, а не вдвое больше шансов."
        )

    from web.services.settings import missing_credentials

    missing = missing_credentials(db, job.service)
    if missing:
        raise JobStartError(f"Не заполнены доступы сервиса: {', '.join(missing)}")

    # Незакрытая попытка брони в журнале — бот при запуске остановится сам.
    # Говорим об этом заранее, иначе процесс просто завершится через секунду,
    # и человек будет гадать, почему.
    from web.services.bookings import blocking_attempt

    blocker = blocking_attempt(job.service)
    if blocker is not None:
        raise JobStartError(
            f"Автобронь заблокирована незакрытой попыткой ({blocker.status}, "
            f"{blocker.started_at}). Проверьте брони у перевозчика и снимите "
            f"блокировку в разделе «Брони»."
        )

    try:
        launch = prepare_launch(db, job)
    except ValueError as exc:
        raise JobStartError(str(exc)) from exc

    job.run_dir = str(launch.run_dir)
    job.status = JobStatus.RUNNING.value
    job.started_at = dt.datetime.now()
    job.finished_at = None
    job.exit_code = None
    job.error = ""
    db.commit()

    env = os.environ.copy()
    # Бот печатает по-русски; на Windows без этого вывод приходит в cp1251
    # и превращается в мусор ещё до того, как мы его прочитаем.
    env["PYTHONIOENCODING"] = "utf-8"
    # Иначе строки копятся в буфере и лог в панели отстаёт на минуты.
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_COLOR"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT)

    try:
        process = subprocess.Popen(
            [_python_executable(), *launch.args],
            cwd=str(launch.cwd),
            env=env,
            stdout=subprocess.PIPE,
            # Ошибки бота должны попасть в тот же лог, что и обычный вывод:
            # разбирать два потока порознь при разборе неудачи невозможно.
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **_popen_kwargs(),
        )
    except OSError as exc:
        job.status = JobStatus.FAILED.value
        job.error = f"не удалось запустить: {exc}"
        job.finished_at = dt.datetime.now()
        db.commit()
        raise JobStartError(job.error) from exc

    job.pid = process.pid
    db.commit()

    with _lock:
        _processes[job.id] = process

    _log(db, job.id, f"Запущен процесс {process.pid}: {' '.join(launch.args)}")

    warning = local_env_warning(job.service)
    if warning:
        _log(db, job.id, warning, LogLevel.WARN.value)

    # Читаем вывод в отдельном потоке: чтение блокирующее, а запрос
    # на запуск должен вернуться сразу.
    reader = threading.Thread(
        target=_pump_output,
        args=(job.id, process),
        name=f"job-{job.id}-output",
        daemon=True,
    )
    reader.start()


# --------------------------------------------------------------------------- #
# Чтение вывода
# --------------------------------------------------------------------------- #


def _pump_output(job_id: int, process: subprocess.Popen) -> None:
    """
    Построчно перекладывать вывод бота в базу. Работает в отдельном потоке.

    Своя сессия: сессия запроса к этому моменту давно закрыта.
    """
    written = 0
    try:
        for raw in process.stdout:  # type: ignore[union-attr]
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue

            if written >= MAX_LOGS_PER_JOB:
                # Не обрываем чтение: процесс должен доработать. Просто
                # перестаём писать, иначе база растёт бесконечно.
                continue

            try:
                with session_scope() as db:
                    db.add(JobLog(job_id=job_id, text=line[:4000], level=classify(line)))
                written += 1
                if written == MAX_LOGS_PER_JOB:
                    with session_scope() as db:
                        db.add(
                            JobLog(
                                job_id=job_id,
                                level=LogLevel.WARN.value,
                                text=f"Достигнут предел в {MAX_LOGS_PER_JOB} строк — "
                                     f"дальнейший вывод не сохраняется",
                            )
                        )
            except Exception as exc:  # noqa: BLE001 — база не должна ронять чтение
                print(f"[runner] job {job_id}: не записал строку лога: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] job {job_id}: чтение вывода прервано: {exc}")
    finally:
        code = process.wait()
        _finish(job_id, code)


# 0xC000013A — код, которым Windows завершает процесс по Ctrl+Break, если тот
# не успел обработать сигнал сам. Для нас это всё равно «остановлен человеком».
WINDOWS_CONTROL_C_EXIT = 0xC000013A

_EXIT_MEANING = {
    0: (JobStatus.BOOKED.value, "Готово: места найдены или бронь создана"),
    WINDOWS_CONTROL_C_EXIT: (JobStatus.STOPPED.value, "Остановлен"),
    2: (
        JobStatus.NEEDS_REVIEW.value,
        "Бронь под вопросом: подтверждение отправлено, ответа нет. "
        "Проверьте брони у перевозчика ПЕРЕД повторным запуском.",
    ),
    130: (JobStatus.STOPPED.value, "Остановлен"),
}


def _finish(job_id: int, code: int) -> None:
    """Проставить итог процесса и подобрать результат брони из журнала бота."""
    with _lock:
        _processes.pop(job_id, None)

    status, message = _EXIT_MEANING.get(code, (JobStatus.FAILED.value, f"Завершился с кодом {code}"))

    try:
        with session_scope() as db:
            job = db.get(Job, job_id)
            if job is None:
                return

            # Остановленный человеком выходит с кодом 0 (бот корректно закрылся),
            # но это не «места найдены» — не переписываем ни статус, ни сообщение.
            stopped_by_human = job.status == JobStatus.STOPPED.value
            if stopped_by_human:
                message = f"Остановлен по команде (код выхода {code})"
                level = LogLevel.WARN.value
            else:
                job.status = status
                level = LogLevel.OK.value if code == 0 else LogLevel.ERROR.value

            job.exit_code = code
            job.pid = None
            job.finished_at = dt.datetime.now()
            db.add(JobLog(job_id=job_id, level=level, text=message))
            db.commit()

            _collect_bookings(db, job)
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] job {job_id}: не записал итог: {exc}")


def _collect_bookings(db, job: Job) -> None:
    """
    Подобрать результат брони из журнала бота и разослать ссылку на оплату.

    Ссылку берём из журнала, а не из печати в консоль: там она лежит структурно,
    рядом со статусом попытки, и не зависит от того, как бот её отформатировал.

    Ошибки здесь не поднимаем: бронь уже создана, и падение на рассылке было бы
    худшим из исходов — человек не узнал бы про ссылку вообще.
    """
    from web.services.bookings import sync_from_journal
    from web.services.notifications import format_booking_message, notify_booking

    try:
        created = sync_from_journal(db, job)
    except Exception as exc:  # noqa: BLE001
        db.add(
            JobLog(
                job_id=job.id,
                level=LogLevel.WARN.value,
                text=f"Не удалось прочитать журнал броней: {exc}",
            )
        )
        db.commit()
        return

    for booking in created:
        db.add(
            JobLog(
                job_id=job.id,
                level=LogLevel.OK.value if booking.status == "done" else LogLevel.WARN.value,
                text=(
                    f"Журнал броней: {booking.status}"
                    + (f", номер {booking.booking_number}" if booking.booking_number else "")
                    + (f", оплата {booking.payment_url}" if booking.payment_url else "")
                ),
            )
        )
    db.commit()

    # Письмо шлём только по состоявшейся броне со ссылкой. Про «под вопросом»
    # человек уже увидел предупреждение в карточке процесса — дублировать
    # тревогу письмом со ссылкой, которой нет, смысла нет.
    payable = [b for b in created if b.status == "done" and b.payment_url]
    if not payable:
        return

    names = ", ".join(
        f"{link.passenger.last_name} {link.passenger.first_name}"
        for link in sorted(job.passengers, key=lambda x: x.position)
    )
    route = f"{job.source} → {job.destination}, {job.depart_date}"

    for booking in payable:
        subject, body, sms = format_booking_message(
            route=route,
            payment_url=booking.payment_url,
            booking_number=booking.booking_number,
            passengers=names,
        )
        try:
            results = notify_booking(
                db,
                subject=subject,
                body=body,
                sms_text=sms,
                email=job.notify_email,
                phone=job.notify_phone,
                booking_id=booking.id,
            )
        except Exception as exc:  # noqa: BLE001
            results = []
            db.add(
                JobLog(job_id=job.id, level=LogLevel.ERROR.value,
                       text=f"Рассылка не удалась: {exc}")
            )

        for result in results:
            db.add(
                JobLog(
                    job_id=job.id,
                    level=LogLevel.OK.value if result.ok else LogLevel.ERROR.value,
                    text=f"{result.channel}: {result.summary()}",
                )
            )
        db.commit()


# --------------------------------------------------------------------------- #
# Остановка
# --------------------------------------------------------------------------- #


def stop_job(db, job: Job, *, timeout: float = 15.0) -> bool:
    """
    Остановить процесс. True — остановлен, False — его и не было.

    Сначала мягко: боты ловят KeyboardInterrupt и успевают дописать журнал
    броней. Если за отведённое время не вышел — убиваем.
    """
    with _lock:
        process = _processes.get(job.id)

    if process is None or process.poll() is not None:
        # Процесса нет: панель перезапускали, а запись в базе осталась висеть.
        if job.status in (JobStatus.RUNNING.value, JobStatus.QUEUED.value):
            job.status = JobStatus.STOPPED.value
            job.pid = None
            job.finished_at = dt.datetime.now()
            db.commit()
        return False

    job.status = JobStatus.STOPPED.value
    db.commit()
    _log(db, job.id, "Остановка по команде человека", LogLevel.WARN.value)

    try:
        if sys.platform == "win32":
            # CTRL_BREAK — единственный сигнал, который Windows умеет слать
            # чужому процессу; внутри Python он превращается в KeyboardInterrupt.
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
    except (OSError, ValueError) as exc:
        print(f"[runner] job {job.id}: сигнал не доставлен ({exc}) — убиваю")
        process.kill()
        return True

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _log(db, job.id, f"Не завершился за {timeout:.0f}с — принудительная остановка",
             LogLevel.WARN.value)
        process.kill()

    return True


def recover_orphans(db) -> int:
    """
    Починить записи, оставшиеся «в работе» после падения панели.

    Процессы умирают вместе с панелью, а строки в базе остаются: без этого
    очередь сервиса оказалась бы занята навсегда, и запустить поиск стало бы
    невозможно.
    """
    stale = (
        db.query(Job)
        .filter(Job.status.in_((JobStatus.RUNNING.value, JobStatus.QUEUED.value)))
        .all()
    )

    fixed = 0
    for job in stale:
        if is_running(job.id):
            continue
        job.status = JobStatus.STOPPED.value
        job.pid = None
        job.finished_at = job.finished_at or dt.datetime.now()
        db.add(
            JobLog(
                job_id=job.id,
                level=LogLevel.WARN.value,
                text="Панель была перезапущена — процесс не пережил её остановку",
            )
        )
        fixed += 1

    if fixed:
        db.commit()
    return fixed


def stop_all() -> None:
    """Остановить все процессы при выключении панели."""
    with _lock:
        processes = list(_processes.items())

    for job_id, process in processes:
        if process.poll() is not None:
            continue
        print(f"[runner] останавливаю job {job_id} (pid {process.pid})")
        try:
            if sys.platform == "win32":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            process.kill()
