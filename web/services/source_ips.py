"""Исходящие адреса машины для самолётов: список, привязка аккаунтов, занятость.

На сервере несколько адресов, у каждого свой внешний IP. Сайт режет запросы по
внешнему адресу, поэтому:

* аккаунт привязан к своему адресу (`Provider.source_ip`) и всегда ходит с него;
* с одного адреса одновременно идёт один процесс;
* лестница 429 своя у каждого адреса (`web.services.rate_limit`).

Адрес «по умолчанию» — не отдельный IP, а тот, который система выбирает сама.
Поэтому сравнивается всегда реальный адрес (`effective_ip`): аккаунт «по
умолчанию» и аккаунт, явно привязанный к тому же IP, — это один адрес, и
работать одновременно они не должны.
"""

from __future__ import annotations

from typing import Any

from core.netaddr import SourceIpError, can_bind, default_source_ip, normalize_ip, public_ip
from web.models import Job, JobStatus, Provider, Service

__all__ = [
    "SETTING_KEY",
    "configured_ips",
    "save_ips_text",
    "ips_text",
    "default_ip",
    "effective_ip",
    "ip_title",
    "busy_ips",
    "ips_view",
    "check_public_ip",
    "SourceIpError",
]

SETTING_KEY = "airlines_source_ips"


def configured_ips(db) -> list[dict[str, str]]:
    """Адреса из настроек: [{"ip", "label"}], в порядке, как их записали."""
    from web.services.settings import get_setting

    out: list[dict[str, str]] = []
    for line in get_setting(db, SETTING_KEY).splitlines():
        parts = line.strip().split(maxsplit=1)
        if not parts:
            continue
        try:
            ip = normalize_ip(parts[0])
        except SourceIpError:
            continue  # сохранённое уже проверено; мусор сюда попасть не мог
        out.append({"ip": ip, "label": parts[1].strip() if len(parts) > 1 else ""})
    return out


def ips_text(db) -> str:
    return "\n".join(f"{item['ip']} {item['label']}".strip() for item in configured_ips(db))


def save_ips_text(db, text: str) -> list[dict[str, str]]:
    """
    Сохранить список «IP подпись» по строке на адрес. Бросает SourceIpError.

    Адрес, к которому привязан аккаунт, убрать нельзя: аккаунт молча ушёл бы
    на адрес по умолчанию, то есть на чужой IP.
    """
    from web.services.settings import save_settings

    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for number, line in enumerate((text or "").splitlines(), start=1):
        parts = line.strip().split(maxsplit=1)
        if not parts:
            continue
        try:
            ip = normalize_ip(parts[0])
        except SourceIpError as exc:
            raise SourceIpError(f"Строка {number}: {exc}") from exc
        if ip in seen:
            raise SourceIpError(f"Строка {number}: адрес {ip} уже есть в списке")
        seen.add(ip)
        items.append({"ip": ip, "label": parts[1].strip()[:64] if len(parts) > 1 else ""})

    bound = (
        db.query(Provider)
        .filter(Provider.service == Service.AIRLINES.value, Provider.source_ip != "")
        .all()
    )
    for provider in bound:
        if provider.source_ip not in seen:
            raise SourceIpError(
                f"К адресу {provider.source_ip} привязан аккаунт «{provider.title}» — "
                f"сначала переведите аккаунт на другой адрес"
            )

    save_settings(db, {SETTING_KEY: "\n".join(f"{i['ip']} {i['label']}".strip() for i in items)})
    return items


def default_ip() -> str:
    """Адрес, который система берёт, если его не указать. Пусто — сети нет."""
    return default_source_ip()


def effective_ip(provider: Provider | None) -> str:
    """Реальный исходящий адрес аккаунта: свой или адрес по умолчанию."""
    if provider is not None and provider.source_ip:
        return provider.source_ip
    return default_ip()


def ip_title(db, ip: str) -> str:
    """«172.16.16.214 (95.95.95.95)» — адрес с подписью из настроек."""
    if not ip:
        return "по умолчанию"
    for item in configured_ips(db):
        if item["ip"] == ip and item["label"]:
            return f"{ip} ({item['label']})"
    return ip


def busy_ips(db, *, exclude_job_id: int | None = None) -> dict[str, int]:
    """Адрес → процесс самолётов, который с него сейчас идёт."""
    query = db.query(Job).filter(
        Job.service == Service.AIRLINES.value,
        Job.status == JobStatus.RUNNING.value,
        Job.source_ip != "",
    )
    if exclude_job_id is not None:
        query = query.filter(Job.id != exclude_job_id)
    return {job.source_ip: job.id for job in query.all()}


def ips_view(db) -> list[dict[str, Any]]:
    """Адреса для страницы настроек: состояние, аккаунты, лестница 429."""
    from web.services.rate_limit import status_view

    default = default_ip()
    items = [dict(item, configured=True) for item in configured_ips(db)]
    if default and default not in {item["ip"] for item in items}:
        # Адрес по умолчанию показываем всегда: с него ходят аккаунты без привязки.
        # Выбрать его аккаунту явно нельзя, пока его нет в списке.
        items = items + [{"ip": default, "label": "", "configured": False}]

    providers = (
        db.query(Provider).filter(Provider.service == Service.AIRLINES.value)
        .order_by(Provider.id).all()
    )
    busy = busy_ips(db)
    out: list[dict[str, Any]] = []
    for item in items:
        ip = item["ip"]
        out.append(
            {
                "ip": ip,
                "label": item["label"],
                "configured": item["configured"],
                "is_default": ip == default,
                "on_machine": can_bind(ip),
                "busy_job": busy.get(ip),
                "accounts": [p.title for p in providers if effective_ip(p) == ip],
                "rate_limit": status_view(ip),
            }
        )
    return out


def check_public_ip(ip: str) -> str:
    """Внешний IP за адресом — человеку в сообщение. Ошибку превращает в текст."""
    if ip and not can_bind(ip):
        return f"Адреса {ip} нет на этой машине"
    try:
        seen = public_ip(ip)
    except OSError as exc:
        return f"{ip or 'по умолчанию'}: не удалось узнать внешний IP — {exc}"
    return f"{ip or 'по умолчанию'} → внешний IP {seen}"
