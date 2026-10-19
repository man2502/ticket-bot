"""Тесты исходящих адресов: проверка IP и привязка соединения к адресу.

Запуск (из корня репозитория):
    python run_tests.py test_core_netaddr.py
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "airlines_api")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from core import netaddr  # noqa: E402
from core.http import HttpClient  # noqa: E402

# Адрес из документационного диапазона: его точно нет ни на одной машине.
FOREIGN_IP = "203.0.113.7"


def test_normalize_ip() -> None:
    assert netaddr.normalize_ip(" 172.16.16.214 ") == "172.16.16.214"
    assert netaddr.normalize_ip("") == ""
    with pytest.raises(netaddr.SourceIpError):
        netaddr.normalize_ip("172.16.16")


def test_slug_is_safe_for_file_names() -> None:
    assert netaddr.ip_slug("172.16.16.214") == "172_16_16_214"
    assert netaddr.ip_slug("") == "default"
    assert ":" not in netaddr.ip_slug("fe80::1")


def test_can_bind_knows_local_and_foreign_addresses() -> None:
    assert netaddr.can_bind("127.0.0.1")
    assert not netaddr.can_bind(FOREIGN_IP)
    assert netaddr.can_bind("")  # пусто — адрес выбирает система


def test_connection_goes_out_from_the_chosen_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """Все запросы бота идут через HttpClient — привязка к адресу именно здесь."""
    seen: dict[str, object] = {}

    def fake_create_connection(address, timeout=None, source_address=None):  # noqa: ANN001
        seen["source_address"] = source_address
        raise OSError("дальше не нужно")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)

    client = HttpClient("https://example.com", source_ip="172.16.17.214", max_retries=0)
    client.warmup()
    assert seen["source_address"] == ("172.16.17.214", 0)

    seen.clear()
    HttpClient("https://example.com", max_retries=0).warmup()
    assert seen["source_address"] is None


def test_bot_refuses_to_start_from_a_missing_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без проверки бот падал бы на каждом запросе — или ушёл бы с чужого IP."""
    import api_check

    monkeypatch.setenv("API_SOURCE_IP", FOREIGN_IP)
    assert FOREIGN_IP in api_check.check_source_ip()

    monkeypatch.setenv("API_SOURCE_IP", "127.0.0.1")
    assert api_check.check_source_ip() == ""

    monkeypatch.setenv("API_SOURCE_IP", "")
    assert api_check.check_source_ip() == ""
