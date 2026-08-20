"""Проверка auth-ошибок API T5 (Bearer-токены, без cookies)."""

from __future__ import annotations


def is_auth_forbidden(status: int, body: dict | str) -> bool:
    if status in (401, 403, 419):
        return True
    if isinstance(body, dict):
        msg = str(body.get("message") or body.get("data") or "").lower()
        if "forbidden" in msg or "unauthorized" in msg or "token" in msg and "expired" in msg:
            return True
    if isinstance(body, str) and "forbidden" in body.lower():
        return True
    return False


def print_auth_help() -> None:
    print(
        "[auth] ⚠ 401/403/forbidden — протухли Bearer-токены.\n"
        "  Если заданы API_CUSTOMER_PHONE + API_CUSTOMER_PASSWORD + API_IOS_PASSWORD,\n"
        "  скрипт снимет GET /customers/info; 401 с token_type=client|customer\n"
        "  обновит только этот Bearer (API_AUTO_REFRESH_TOKENS=1).\n"
        "  Иначе обнови вручную в airlines_api/.env.local:\n"
        "  • API_AUTHORIZATION / API_CUSTOMER_AUTHORIZATION"
    )
