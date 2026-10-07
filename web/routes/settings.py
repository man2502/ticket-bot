"""Раздел «Настройки» — почта, SMS-шлюз и параметры сервисов. Только админ.

Секреты обрабатываются по одному правилу во всех формах: сохранённое значение
никогда не отдаётся в HTML, пустое поле означает «не менять», а стереть можно
явным дефисом. Иначе правка соседней галочки обнуляла бы пароль.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from web.auth import require_admin
from web.db import get_db
from web.models import NotificationLog, Service, User
from web.services.notifications import (
    API_KEY_PLACES,
    send_email,
    send_sms,
    sms_configured,
    smtp_configured,
)
from web.services.rate_limit import clear as clear_rate_limit
from web.services.rate_limit import status_view as rate_limit_status
from web.services.providers import (
    ProviderError,
    delete_provider,
    get_provider,
    providers_view,
    save_provider,
)
from web.services.settings import (
    get_all,
    get_setting,
    missing_credentials,
    save_service_config,
    save_settings,
    service_config_view,
)
from web.templating import render

router = APIRouter(prefix="/settings", tags=["settings"])

SERVICE_TITLES = {
    Service.AIRLINES.value: "Самолёты (turkmenistanairlines.tm)",
    Service.RAILWAY.value: "Поезда (railway.gov.tm)",
}

SMTP_FIELDS = ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from")
SMS_FIELDS = (
    "sms_url",
    "sms_api_key",
    "sms_api_key_field",
    "sms_api_key_place",
    "sms_phone_field",
    "sms_message_field",
)
RECIPIENT_FIELDS = ("default_notify_email", "default_notify_phone")


def _page(
    request: Request,
    user: User,
    db: Session,
    *,
    message: str | None = None,
    is_error: bool = False,
) -> HTMLResponse:
    services = []
    for service in Service:
        view = service_config_view(db, service.value)
        view["title"] = SERVICE_TITLES[service.value]
        view["missing"] = missing_credentials(db, service.value)
        # Авторизации у railway нет — API открытый, полям доступа взяться неоткуда.
        view["needs_auth"] = service == Service.AIRLINES
        view["has_captcha"] = service == Service.AIRLINES
        view["providers"] = providers_view(db, service.value) if view["needs_auth"] else []
        view["rate_limit"] = rate_limit_status() if view["needs_auth"] else None
        services.append(view)

    return render(
        request,
        "settings.html",
        user,
        status_code=422 if is_error else 200,
        values=get_all(db),
        services=services,
        api_key_places=API_KEY_PLACES,
        smtp_ready=smtp_configured(db),
        sms_ready=sms_configured(db),
        message=message,
        is_error=is_error,
        recent_sends=(
            db.query(NotificationLog)
            .order_by(NotificationLog.created_at.desc())
            .limit(10)
            .all()
        ),
    )


async def _fields(request: Request, names: tuple[str, ...]) -> dict[str, str]:
    raw = await request.form()
    return {name: str(raw.get(name, "")) for name in names}


@router.get("", response_class=HTMLResponse)
def settings_page(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _page(request, user, db)


# --------------------------------------------------------------------------- #
# Почта
# --------------------------------------------------------------------------- #


@router.post("/smtp")
async def save_smtp(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    save_settings(db, await _fields(request, SMTP_FIELDS))
    return _page(request, user, db, message="Настройки почты сохранены")


@router.post("/smtp/test")
async def test_smtp(
    request: Request,
    to: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = to.strip() or get_setting(db, "default_notify_email")
    if not target:
        return _page(
            request, user, db, is_error=True,
            message="Укажите адрес для проверки или заполните получателя по умолчанию",
        )

    result = send_email(
        db,
        to=target,
        subject="Проверка связи — T-sender",
        body=(
            "Это тестовое письмо из T-sender.\n\n"
            "Если оно дошло, ссылка на оплату брони тоже дойдёт."
        ),
    )
    return _page(request, user, db, message=f"Почта: {result.summary()}", is_error=not result.ok)


# --------------------------------------------------------------------------- #
# SMS
# --------------------------------------------------------------------------- #


@router.post("/sms")
async def save_sms(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    save_settings(db, await _fields(request, SMS_FIELDS))
    return _page(request, user, db, message="Настройки SMS сохранены")


@router.post("/sms/test")
async def test_sms(
    request: Request,
    phone: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = phone.strip() or get_setting(db, "default_notify_phone")
    if not target:
        return _page(
            request, user, db, is_error=True,
            message="Укажите номер для проверки или заполните получателя по умолчанию",
        )

    result = send_sms(db, phone=target, message="Проверка связи — T-sender")
    return _page(request, user, db, message=f"SMS: {result.summary()}", is_error=not result.ok)


# --------------------------------------------------------------------------- #
# Получатели по умолчанию
# --------------------------------------------------------------------------- #


@router.post("/recipients")
async def save_recipients(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from web.services.passengers import normalize_phone

    data = await _fields(request, RECIPIENT_FIELDS)
    # Номер приводим к одному виду сразу: SMS должна уходить по номеру,
    # а не по тому, что человек успел набрать.
    if data.get("default_notify_phone"):
        data["default_notify_phone"] = normalize_phone(data["default_notify_phone"])

    save_settings(db, data)
    return _page(request, user, db, message="Получатели по умолчанию сохранены")


# --------------------------------------------------------------------------- #
# Настройки сервисов
# --------------------------------------------------------------------------- #


@router.post("/service/{service}")
async def save_service(
    service: str,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if service not in {s.value for s in Service}:
        return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)

    raw = await request.form()
    # Телефона и пароля личного кабинета в этой форме больше нет — они
    # у аккаунтов (провайдеров) ниже. Не передаём их, чтобы сохранение
    # остальных настроек не затирало старые значения пустыми.
    data: dict[str, object] = {
        "extra_login": str(raw.get("extra_login", "")),
        "extra_password_enc": str(raw.get("extra_password", "")),
        "max_parallel": str(raw.get("max_parallel", "2")),
        "check_period_sec": str(raw.get("check_period_sec", "30")),
        "check_at_second": str(raw.get("check_at_second", "0")),
        "max_searches_per_hour": str(raw.get("max_searches_per_hour", "0")),
        "repeated_error_pause_after": str(raw.get("repeated_error_pause_after", "3")),
        "rate_limit_pause_sec": str(raw.get("rate_limit_pause_sec", "150")),
        "captcha_max_attempts": str(raw.get("captcha_max_attempts", "5")),
        # Флажки приходят в форме, только если отмечены.
        "is_enabled": "is_enabled" in raw,
        "captcha_manual": "captcha_manual" in raw,
        "auto_book": "auto_book" in raw,
    }
    if service == Service.AIRLINES.value:
        # Галочка есть только у самолётов. Для поездов её не передаём,
        # иначе сохранение их формы молча выключало бы настройку.
        data["allow_shared_passengers"] = "allow_shared_passengers" in raw

    save_service_config(db, service, data)
    return _page(request, user, db, message=f"{SERVICE_TITLES[service]}: настройки сохранены")


# --------------------------------------------------------------------------- #
# Аккаунты перевозчика
# --------------------------------------------------------------------------- #


async def _provider_form(request: Request) -> dict[str, object]:
    raw = await request.form()
    return {
        "name": str(raw.get("name", "")),
        "login": str(raw.get("login", "")),
        "password": str(raw.get("password", "")),
        "check_at_second": str(raw.get("check_at_second", "")),
        # Флажок приходит в форме, только если отмечен.
        "is_enabled": "is_enabled" in raw,
    }


@router.post("/rate-limit/clear")
def rate_limit_clear(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Снять стоп после 429 раньше срока — только по решению человека."""
    return _page(request, user, db, message=clear_rate_limit())


@router.post("/providers")
async def create_provider(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    data = await _provider_form(request)
    if not str(data["login"]).strip():
        return _page(request, user, db, is_error=True,
                     message="Укажите телефон аккаунта T5")
    if not str(data["password"]).strip():
        return _page(request, user, db, is_error=True,
                     message="Укажите пароль аккаунта T5")
    provider = save_provider(db, None, data)
    return _page(request, user, db, message=f"Аккаунт «{provider.title}» добавлен")


@router.post("/providers/{provider_id}")
async def update_provider(
    provider_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    provider = get_provider(db, provider_id)
    if provider is None:
        return _page(request, user, db, is_error=True, message="Аккаунт не найден")
    data = await _provider_form(request)
    provider = save_provider(db, provider, data)
    # Работающий процесс свой check.env уже прочитал — правка подействует
    # со следующего запуска. Говорим об этом, чтобы не ждали эффекта сразу.
    return _page(request, user, db,
                 message=f"Аккаунт «{provider.title}» сохранён. Работающие процессы "
                         f"подхватят изменения после перезапуска.")


@router.post("/providers/{provider_id}/delete")
def remove_provider(
    provider_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    provider = get_provider(db, provider_id)
    if provider is None:
        return _page(request, user, db, is_error=True, message="Аккаунт не найден")
    try:
        result = delete_provider(db, provider)
    except ProviderError as exc:
        return _page(request, user, db, is_error=True, message=str(exc))
    return _page(request, user, db, message=result)
