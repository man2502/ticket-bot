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
import re
import signal
import subprocess
import sys
import threading

from web.config import REPO_ROOT
from web.db import session_scope
from web.models import Job, JobLog, JobStatus, LogLevel, Service
from web.services.bot_env import cleanup_run_dir, local_env_warning, prepare_launch

__all__ = [
    "start_job",
    "stop_job",
    "delete_job",
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
_ERROR_TAGS = ("[err]", "[error]", "[auth]", "[fatal]")
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


def _assign_provider(db, job: Job) -> None:
    """
    Выбрать аккаунт для процесса самолётов. Бросает JobStartError с причиной.

    Если человек выбрал аккаунт сам (`params["provider"]`), берём только его.
    Иначе — первый свободный: включённый, с заполненными доступами, не занятый
    другим процессом и без незакрытой попытки брони в своём журнале (с ней
    бот остановится сразу после старта).

    Выбор хранится в `job.provider_id`, но при каждом запуске делается заново,
    если человек просил «любой»: иначе перезапуск остановленного процесса
    упирался бы в аккаунт, который тем временем занял кто-то другой.
    """
    from web.services.bookings import provider_blocker
    from web.services.providers import (
        busy_provider_ids,
        get_provider,
        list_providers,
        provider_missing,
        running_count,
    )
    from web.services.settings import get_service_config

    config = get_service_config(db, job.service)
    running = running_count(db, job.service, exclude_job_id=job.id)
    if running >= config.max_parallel:
        raise JobStartError(
            f"Уже работает процессов самолётов: {running} — это предел из настроек "
            f"({config.max_parallel}). Все аккаунты ходят с одного адреса, и сайт "
            f"режет запросы по адресу."
        )

    busy = busy_provider_ids(db, exclude_job_id=job.id)

    def problem_with(provider) -> str:
        if not provider.is_enabled:
            return "выключен"
        missing = provider_missing(provider)
        if missing:
            return "не заполнено: " + ", ".join(missing)
        if provider.id in busy:
            return f"занят процессом #{busy[provider.id]}"
        blocker = provider_blocker(db, provider.id)
        if blocker is not None:
            record = blocker.record
            return (
                f"в журнале незакрытая попытка брони ({record.status}, {record.started_at})"
            )
        return ""

    chosen_id = str(job.params.get("provider") or "").strip()
    if chosen_id:
        provider = get_provider(db, int(chosen_id)) if chosen_id.isdigit() else None
        if provider is None:
            raise JobStartError("Выбранный аккаунт перевозчика удалён — выберите другой")
        problem = problem_with(provider)
        if problem:
            raise JobStartError(f"Аккаунт «{provider.title}»: {problem}")
        job.provider = provider
        return

    providers = list_providers(db, job.service)
    if not providers:
        raise JobStartError(
            "Нет ни одного аккаунта перевозчика. Добавьте его в «Настройки → Самолёты»."
        )

    reasons: list[str] = []
    for provider in providers:
        problem = problem_with(provider)
        if not problem:
            job.provider = provider
            return
        reasons.append(f"«{provider.title}» — {problem}")

    raise JobStartError("Нет свободного аккаунта: " + "; ".join(reasons))


def _missing_booking_contact(db, job: Job) -> str:
    """
    Проверить контакты для брони заранее. Пустая строка — всё в порядке.

    Сервер T5 отвергает CreateBooking без contactEmail с невнятным
    `Error Create (HTTP 404)`, и узнать об этом в момент появления мест —
    значит потерять именно тот слот, ради которого всё и затевалось.

    При выключенной автоброни контакты не нужны: бот только смотрит и
    сообщает, а бронирует человек руками.
    """
    from web.services.bot_env import booking_contacts
    from web.services.settings import get_service_config

    if not get_service_config(db, job.service).auto_book:
        return ""

    email, phone = booking_contacts(db, job)
    # Почта и телефон пассажира — законный запасной вариант: бот подставит сам.
    email = email or any((link.passenger.email or "").strip() for link in job.passengers)
    phone = phone or any((link.passenger.phone or "").strip() for link in job.passengers)

    if job.service == Service.RAILWAY.value:
        # Поездам нужны оба поля: без любого сервер отвечает input.invalidInputParam.
        missing = [name for name, value in (("email", email), ("телефон", phone)) if not value]
        if not missing:
            return ""
        return (
            f"Не задан {' и '.join(missing)} для брони. Сервер перевозчика без них отказывает. "
            "Заполните «Куда слать ссылку на оплату» и телефон в настройках панели, "
            "в самом процессе или у пассажира."
        )

    if email:
        return ""

    return (
        "Не задан email для брони. Сервер перевозчика без него отказывает. "
        "Заполните «Куда слать ссылку на оплату» в настройках панели, "
        "адрес в самом процессе или email пассажира."
    )


def start_job(db, job: Job) -> None:
    """
    Запустить процесс поиска. Бросает `JobStartError`, если запускать нельзя.

    Проверки очереди делаются здесь, а не в роуте: запуск возможен и из других
    мест (повтор, автозапуск), а правило «один процесс на сервис» должно
    соблюдаться всегда.
    """
    if is_running(job.id):
        raise JobStartError("Процесс уже запущен")

    if job.service == Service.AIRLINES.value:
        # У самолётов очередь не на сервис, а на аккаунт: процессов может быть
        # столько, сколько аккаунтов (но не больше предела из настроек).
        _assign_provider(db, job)
    else:
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

    contact_problem = _missing_booking_contact(db, job)
    if contact_problem:
        raise JobStartError(contact_problem)

    if job.service == Service.AIRLINES.value:
        from web.services.rate_limit import start_blocker as rate_limit_blocker

        stopped = rate_limit_blocker()
        if stopped:
            raise JobStartError(stopped)

    # Незакрытая попытка брони в журнале — бот при запуске остановится сам.
    # Говорим об этом заранее, иначе процесс просто завершится через секунду,
    # и человек будет гадать, почему.
    if job.service == Service.AIRLINES.value:
        from web.services.bookings import job_start_blocker

        problem = job_start_blocker(db, job)
        if problem:
            raise JobStartError(problem)
    else:
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
                    _record_error_event(db, job_id, line)
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


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def parse_error_line(line: str) -> dict | None:
    """
    Разобрать строку журнала ошибок бота. None — это не такая строка.

        [err] rate_limit | HTTP 429 | customers/info | Retry-After=900с | …

    Подробности могут сами содержать « | » (как у 429), поэтому всё после
    третьего разделителя — одно поле.
    """
    text = _ANSI.sub("", line).strip()
    if not text.startswith("[err] "):
        return None
    parts = [part.strip() for part in text[len("[err] "):].split(" | ", 3)]
    key = parts[0] if parts and parts[0] else "other"
    status = None
    if len(parts) > 1 and parts[1].startswith("HTTP "):
        try:
            status = int(parts[1][5:].strip())
        except ValueError:
            status = None
    endpoint = parts[2] if len(parts) > 2 and parts[2] != "—" else ""
    detail = parts[3] if len(parts) > 3 else (parts[1] if len(parts) == 2 else "")
    return {"key": key[:32], "status": status, "endpoint": endpoint[:128], "detail": detail[:2000]}


def _record_error_event(db, job_id: int, line: str) -> None:
    from web.models import ErrorEvent

    parsed = parse_error_line(line)
    if parsed is None:
        return
    job = db.get(Job, job_id)
    db.add(
        ErrorEvent(
            job_id=job_id,
            provider_id=job.provider_id if job else None,
            service=job.service if job else "",
            **parsed,
        )
    )


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
    # Пятый 429 по общей лестнице: остановлены все процессы самолётов.
    3: (
        JobStatus.FAILED.value,
        "Остановлен после повторных 429 вместе со всеми процессами самолётов. "
        "Не перезапускайте сразу — это и есть путь к блокировке аккаунта.",
    ),
}

RATE_LIMIT_EXIT_CODE = 3


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
            if code == RATE_LIMIT_EXIT_CODE and not stopped_by_human:
                _notify_rate_limit_stop(db, job)
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] job {job_id}: не записал итог: {exc}")


def _notify_rate_limit_stop(db, job: Job) -> None:
    """
    Сообщить человеку о стопе после 429. Один раз на стоп, а не от каждого процесса.

    Процессы останавливаются вместе, и каждый выходит с кодом 3 — без этой
    проверки пришло бы столько писем, сколько было процессов.
    """
    from web.services.notifications import notify_booking
    from web.services.rate_limit import status_view, stop_notice_key
    from web.services.settings import get_setting, save_settings

    key = stop_notice_key()
    if not key or get_setting(db, "rate_limit_notified") == key:
        return
    save_settings(db, {"rate_limit_notified": key})

    view = status_view()
    retry = (
        f"{view['last_retry_after']:.0f} с" if view["last_retry_after"] is not None else "не прислал"
    )
    subject = "Самолёты остановлены: повторные 429"
    body = "\n".join(
        [
            "Сайт перевозчика несколько раз ответил «слишком много запросов» (HTTP 429).",
            "Все процессы самолётов остановлены, чтобы аккаунты не заблокировали.",
            "",
            f"Стоп: {view['headline']}",
            f"Последний 429: {view['last_429_at']}, аккаунт {view['last_source'] or '—'}, "
            f"запрос {view['last_endpoint'] or '—'}",
            f"Retry-After от сервера: {retry}",
            "",
            "Не перезапускайте процессы раньше времени.",
        ]
    )
    try:
        results = notify_booking(
            db, subject=subject, body=body, sms_text=f"{subject}. {view['headline']}"
        )
    except Exception as exc:  # noqa: BLE001 — уведомление не должно ронять итог процесса
        db.add(JobLog(job_id=job.id, level=LogLevel.ERROR.value,
                      text=f"Уведомление о стопе не ушло: {exc}"))
        db.commit()
        return
    for result in results:
        db.add(JobLog(job_id=job.id,
                      level=LogLevel.OK.value if result.ok else LogLevel.ERROR.value,
                      text=f"Уведомление о стопе — {result.channel}: {result.summary()}"))
    db.commit()


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
# Удаление
# --------------------------------------------------------------------------- #


def delete_job(db, job: Job) -> None:
    """
    Удалить процесс вместе с логами и пассажирами. Брони остаются.

    Перед удалением развязываем ссылки, у которых нет каскада:

    * `bookings.job_id` — бронь существует у перевозчика и за неё выставлен
      счёт. Удалять её вместе с поиском нельзя: человек стёр бы ссылку
      на оплату, ничего про это не поняв. Бронь остаётся в разделе «Брони»
      без процесса.
    * `captcha_samples.job_id` — размеченная капча остаётся в датасете
      независимо от того, жив ли процесс, при котором она появилась.

    История отправок (`notification_logs`) не трогается вовсе: она привязана
    к брони, а бронь теперь переживает удаление процесса.

    В новых базах это делает `ondelete="SET NULL"`, но существующие им не
    исправить: SQLite не умеет менять внешние ключи через ALTER TABLE.
    Поэтому развязываем явно — работает на любой базе.
    """
    from web.models import Booking, CaptchaSample

    # synchronize_session="fetch", а не False: сессия живёт с
    # expire_on_commit=False, и уже загруженные брони остались бы в памяти
    # со старым job_id — страница показала бы их принадлежащими удалённому поиску.
    (
        db.query(Booking)
        .filter(Booking.job_id == job.id)
        .update({Booking.job_id: None}, synchronize_session="fetch")
    )

    (
        db.query(CaptchaSample)
        .filter(CaptchaSample.job_id == job.id)
        .update({CaptchaSample.job_id: None}, synchronize_session=False)
    )

    # В каталоге запуска лежит check.env с паролями — убираем вместе с процессом.
    cleanup_run_dir(job)

    db.delete(job)
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
