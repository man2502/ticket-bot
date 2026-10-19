"""Исходящие адреса: с какого IP машины бот ходит к перевозчику.

На сервере может быть несколько адресов, и у каждого свой внешний IP (NAT).
Сайт считает запросы по внешнему адресу, поэтому аккаунт привязывается к
своему IP, а лестница 429 ведётся отдельно на каждый адрес.

Здесь только сетевые мелочи без панели и без бота: проверить, что адрес есть
на машине, узнать адрес «по умолчанию» и внешний IP за адресом.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl

__all__ = [
    "normalize_ip",
    "can_bind",
    "default_source_ip",
    "public_ip",
    "ip_slug",
    "SourceIpError",
]

# Сервис, который отвечает внешним IP одной строкой.
PUBLIC_IP_HOST = "api.ipify.org"


class SourceIpError(ValueError):
    """Адрес не годится как исходящий — с объяснением для человека."""


def normalize_ip(raw: str) -> str:
    """Строка адреса в каноническом виде; пусто — пусто. Мусор — SourceIpError."""
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        return str(ipaddress.ip_address(text))
    except ValueError as exc:
        raise SourceIpError(f"{text!r} — не IP-адрес") from exc


def _family(ip: str) -> int:
    return socket.AF_INET6 if ipaddress.ip_address(ip).version == 6 else socket.AF_INET


def can_bind(ip: str) -> bool:
    """Есть ли такой адрес на этой машине. Без отправки чего-либо в сеть."""
    if not ip:
        return True
    try:
        with socket.socket(_family(ip), socket.SOCK_STREAM) as sock:
            sock.bind((ip, 0))
    except (OSError, ValueError):
        return False
    return True


def default_source_ip() -> str:
    """
    С какого адреса система уходит в интернет, если адрес не указан.

    UDP-«соединение» ничего не отправляет: ядро только выбирает маршрут и
    исходящий адрес. Пусто — сети нет или маршрута по умолчанию нет.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 53))
            return sock.getsockname()[0]
    except OSError:
        return ""


def public_ip(source_ip: str = "", *, timeout: float = 8.0) -> str:
    """Внешний IP, под которым видят запросы с `source_ip`. Бросает OSError при сбое."""
    source = (source_ip, 0) if source_ip else None
    conn = http.client.HTTPSConnection(
        PUBLIC_IP_HOST, timeout=timeout, source_address=source,
        context=ssl.create_default_context(),
    )
    try:
        conn.request("GET", "/", headers={"User-Agent": "curl/8"})
        response = conn.getresponse()
        body = response.read(64).decode("ascii", "replace").strip()
    finally:
        conn.close()
    if response.status != 200:
        raise OSError(f"{PUBLIC_IP_HOST} ответил HTTP {response.status}")
    return normalize_ip(body)


def ip_slug(ip: str) -> str:
    """Адрес для имени файла: 172.16.16.214 → 172_16_16_214; пусто — default."""
    return ip.replace(".", "_").replace(":", "_") if ip else "default"
