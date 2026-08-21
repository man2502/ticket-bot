"""Отправка уведомлений: почта и SMS.

Ссылку на оплату рассылает панель, а не бот. Причины две: настройка живёт
в одном месте, и остаётся история отправок — видно, ушло письмо или нет.
У бота почта при запуске отключается (просто не передаём ему `GMAIL_*`).

SMS-шлюз описывается настройками, а не кодом: у разных провайдеров поля
называются по-разному, а ключ передаётся то в теле, то в заголовке. Поэтому
имена полей и способ передачи ключа задаются в панели.

Ошибки отправки не поднимаются наверх: бронь уже создана, и падение из-за
недоступного SMTP было бы худшим из возможных исходов. Результат возвращается
значением и пишется в журнал отправок.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.mime.text import MIMEText
from email.utils import formataddr
from urllib.parse import urlsplit

from core.http import HttpClient, RequestFailed
from web.services.settings import get_secret, get_setting

__all__ = ["SendResult", "send_email", "send_sms", "notify_booking", "smtp_configured", "sms_configured"]

# Как передаётся ключ SMS-шлюза.
API_KEY_PLACES = (
    ("body", "в теле запроса"),
    ("header", "в заголовке"),
    ("query", "в адресе (query)"),
)


@dataclass
class SendResult:
    ok: bool
    channel: str
    recipient: str = ""
    error: str = ""
    detail: str = ""

    def summary(self) -> str:
        if self.ok:
            return f"Отправлено на {self.recipient}" + (f" ({self.detail})" if self.detail else "")
        return f"Не отправлено: {self.error}"


# --------------------------------------------------------------------------- #
# Почта
# --------------------------------------------------------------------------- #


def smtp_configured(db) -> bool:
    return bool(get_setting(db, "smtp_host") and get_setting(db, "smtp_user"))


def _recipients(raw: str) -> list[str]:
    """Список адресов из строки через запятую или точку с запятой."""
    out: list[str] = []
    for chunk in (raw or "").replace(";", ",").split(","):
        address = chunk.strip()
        if address and "@" in address and address not in out:
            out.append(address)
    return out


def send_email(db, *, to: str, subject: str, body: str) -> SendResult:
    """
    Отправить письмо по настройкам панели.

    Порт 465 — SSL с самого начала, всё остальное — STARTTLS. Различие
    существенное: на 465 порту `starttls()` просто не сработает.
    """
    host = get_setting(db, "smtp_host")
    user = get_setting(db, "smtp_user")
    password = get_secret(db, "smtp_password")
    sender = get_setting(db, "smtp_from") or user

    try:
        port = int(get_setting(db, "smtp_port", "587"))
    except ValueError:
        port = 587

    recipients = _recipients(to)

    if not host or not user or not password:
        return SendResult(
            ok=False, channel="email", recipient=to,
            error="SMTP не настроен: нужны сервер, пользователь и пароль",
        )
    if not recipients:
        return SendResult(ok=False, channel="email", error="не указан получатель")

    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = subject
    message["From"] = formataddr(("Панель билетов", sender))
    message["To"] = ", ".join(recipients)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context()) as smtp:
                smtp.login(user, password)
                smtp.sendmail(sender, recipients, message.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(user, password)
                smtp.sendmail(sender, recipients, message.as_string())
    except smtplib.SMTPAuthenticationError as exc:
        # Отдельно, потому что у Gmail это почти всегда одно и то же:
        # обычный пароль вместо пароля приложения.
        return SendResult(
            ok=False, channel="email", recipient=to,
            error=f"логин отклонён ({exc.smtp_code}). Для Gmail нужен пароль приложения, не обычный",
        )
    except (smtplib.SMTPException, OSError) as exc:
        return SendResult(ok=False, channel="email", recipient=to, error=str(exc))

    return SendResult(ok=True, channel="email", recipient=", ".join(recipients))


# --------------------------------------------------------------------------- #
# SMS
# --------------------------------------------------------------------------- #


def sms_configured(db) -> bool:
    return bool(get_setting(db, "sms_url"))


def send_sms(db, *, phone: str, message: str) -> SendResult:
    """
    Отправить SMS через настроенный шлюз.

    Тело — JSON с полями, имена которых заданы в настройках. Ключ, если он
    вообще нужен, уходит туда, куда указано: в тело, в заголовок или в адрес.
    """
    url = get_setting(db, "sms_url")
    if not url:
        return SendResult(ok=False, channel="sms", recipient=phone, error="SMS-шлюз не настроен")

    phone = (phone or "").strip()
    if not phone:
        return SendResult(ok=False, channel="sms", error="не указан номер")

    phone_field = get_setting(db, "sms_phone_field", "phone")
    message_field = get_setting(db, "sms_message_field", "message")
    key_field = get_setting(db, "sms_api_key_field", "api_key")
    key_place = get_setting(db, "sms_api_key_place", "body")
    api_key = get_secret(db, "sms_api_key")

    payload: dict[str, str] = {phone_field: phone, message_field: message}
    headers = {"accept": "application/json"}
    query: dict[str, str] = {}

    if api_key:
        if key_place == "header":
            headers[key_field] = api_key
        elif key_place == "query":
            query[key_field] = api_key
        else:
            payload[key_field] = api_key

    parts = urlsplit(url)
    if parts.scheme != "https":
        # core.http умеет только https — и это правильно: ключ шлюза
        # не должен уходить открытым текстом.
        return SendResult(
            ok=False, channel="sms", recipient=phone,
            error=f"адрес шлюза должен начинаться с https:// (сейчас {parts.scheme or 'без схемы'})",
        )

    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path or "/"
    client = HttpClient(base, headers_factory=lambda: headers, connect_timeout=5.0,
                        read_timeout=20.0, write_timeout=20.0, name="sms")
    try:
        response = client.request(
            "POST", path, body=payload, query=query or None,
            # Повторять отправку нельзя: шлюз мог принять запрос и отправить SMS,
            # а мы получим второе сообщение вместо одного.
            idempotent=False,
        )
    except RequestFailed as exc:
        return SendResult(ok=False, channel="sms", recipient=phone, error=str(exc))
    finally:
        client.close()

    if response.status >= 400:
        return SendResult(
            ok=False, channel="sms", recipient=phone,
            error=f"HTTP {response.status}: {response.safe_text(200)}",
        )

    return SendResult(
        ok=True, channel="sms", recipient=phone,
        detail=f"HTTP {response.status}, {response.elapsed_ms:.0f} мс",
    )


# --------------------------------------------------------------------------- #
# Уведомление о брони
# --------------------------------------------------------------------------- #


def notify_booking(
    db,
    *,
    subject: str,
    body: str,
    email: str = "",
    phone: str = "",
    sms_text: str = "",
    booking_id: int | None = None,
) -> list[SendResult]:
    """
    Разослать ссылку на оплату по всем настроенным каналам.

    Получатель берётся из процесса, а если там пусто — из общих настроек.
    Каждая попытка пишется в журнал: потом видно, ушло письмо или нет,
    и не приходится гадать, почему человек не увидел ссылку.
    """
    from web.models import NotificationLog

    email = (email or "").strip() or get_setting(db, "default_notify_email")
    phone = (phone or "").strip() or get_setting(db, "default_notify_phone")

    results: list[SendResult] = []

    if email:
        results.append(send_email(db, to=email, subject=subject, body=body))
    if phone:
        # В SMS не влезет всё письмо — отправляем короткий текст со ссылкой.
        results.append(send_sms(db, phone=phone, message=sms_text or body[:300]))

    for result in results:
        db.add(
            NotificationLog(
                booking_id=booking_id,
                channel=result.channel,
                recipient=result.recipient or email or phone,
                subject=subject[:255],
                is_sent=result.ok,
                error=result.error,
            )
        )
    db.commit()

    return results


def format_booking_message(
    *,
    route: str,
    payment_url: str,
    booking_number: str = "",
    passengers: str = "",
) -> tuple[str, str, str]:
    """Тема, тело письма и короткий текст для SMS — в одном месте, чтобы не разошлись."""
    subject = f"Билет забронирован: {route}"
    if booking_number:
        subject = f"Бронь {booking_number}: {route}"

    lines = [route]
    if passengers:
        lines.append(f"Пассажиры: {passengers}")
    if booking_number:
        lines.append(f"Номер брони: {booking_number}")
    lines += ["", "Ссылка для оплаты:", payment_url, "", "Оплатите в течение времени, отведённого перевозчиком."]

    sms = f"Бронь {booking_number or ''} {route}. Оплата: {payment_url}".strip()
    return subject, "\n".join(lines), sms
