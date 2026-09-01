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
    message["From"] = formataddr(("T-sender", sender))
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

    # Кода 200 недостаточно. Шлюзы часто отвечают 200 и на отказ, сообщая
    # о нём в теле — например `{"success":"false","message":"param gelmedi"}`.
    # Поверив одному лишь статусу, панель записала бы «доставлено» там, где
    # SMS не ушла, и человек ждал бы ссылку на оплату впустую.
    failure = _gateway_failure(response.json)
    if failure:
        return SendResult(
            ok=False, channel="sms", recipient=phone,
            error=f"шлюз отказал: {failure}",
        )

    return SendResult(
        ok=True, channel="sms", recipient=phone,
        detail=f"HTTP {response.status}, {response.elapsed_ms:.0f} мс",
    )


# Значения, которыми шлюзы обозначают неудачу. Строка "false" здесь не случайно:
# на JavaScript-бэкендах её пишут вместо булева, и для JS она ещё и «истинна».
_FALSE_VALUES = {"false", "0", "no", "error", "fail", "failed"}


def _gateway_failure(body: object) -> str:
    """
    Текст ошибки, если тело ответа говорит об отказе. Пустая строка — всё хорошо.

    Тело разбираем осторожно: у разных шлюзов поле называется `success`,
    `status` или `error`, а тип значения бывает и булевым, и строковым.
    Непонятный ответ считаем успехом — иначе рабочая отправка помечалась бы
    неудачной из-за неизвестного нам формата.
    """
    if not isinstance(body, dict):
        return ""

    for key in ("success", "status", "ok", "result"):
        if key not in body:
            continue
        value = body[key]
        if value is False or (isinstance(value, str) and value.strip().lower() in _FALSE_VALUES):
            return str(body.get("message") or body.get("error") or f"{key}={value!r}")[:200]

    # Отдельное поле ошибки: непустое значение — это отказ.
    for key in ("error", "err"):
        value = body.get(key)
        if value not in (None, "", 0, False):
            return str(body.get("message") or value)[:200]

    return ""


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
    service: str = "",
    trip: str = "",
    depart_date: str = "",
) -> tuple[str, str, str]:
    """
    Тема, тело письма и короткий текст для SMS — в одном месте, чтобы не разошлись.

    Письмо отвечает на вопрос «что я оплачиваю»: перевозчик, маршрут, дата,
    рейс со временем, места, пассажиры. Раньше в нём были только маршрут
    и ссылка — по ним нельзя было понять даже, утренний это рейс или вечерний,
    а в день их бывает два.

    Номер документа в письмо не кладём намеренно: почтовый ящик — не то место,
    где должны лежать паспортные данные. Для оплаты он и не нужен.
    """
    # Маршрут может быть неизвестен: процесс удалили или бота запускали
    # из командной строки. Тогда в теме — перевозчик, он всяко известен.
    headline = route or service or "поездка"
    subject = f"Билет забронирован: {headline}"
    if booking_number:
        subject = f"Бронь {booking_number}: {headline}"

    fields: list[tuple[str, str]] = []
    if service:
        fields.append(("Перевозчик", service))
    if route:
        fields.append(("Маршрут", route))
    if depart_date:
        fields.append(("Дата", depart_date))
    if trip:
        fields.append(("Рейс", trip))
    if passengers:
        fields.append(("Пассажиры", passengers))
    if booking_number:
        fields.append(("Номер брони", booking_number))

    width = max(len(label) for label, _ in fields)
    lines = [f"{label + ':':<{width + 1}} {value}" for label, value in fields]
    lines += [
        "",
        "Ссылка для оплаты:",
        payment_url,
        "",
        "Оплатите в течение времени, отведённого перевозчиком —",
        "иначе бронь снимается и место уходит.",
    ]

    # В SMS влезает мало, поэтому только то, без чего нельзя: что за поездка
    # и куда платить. Ссылка всегда последней — её не должно обрезать.
    sms_parts = [f"Бронь {booking_number}" if booking_number else "Бронь"]
    if route:
        sms_parts.append(route)
    elif service:
        sms_parts.append(service)
    if depart_date:
        sms_parts.append(depart_date)
    sms_head = " ".join(sms_parts)
    if trip:
        sms_head += f", {trip}"
    sms = f"{sms_head}. Оплата: {payment_url}"
    return subject, "\n".join(lines), sms
