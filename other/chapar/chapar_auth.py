"""Çapar (Turkmenpost) — OTP login на billing.post.gov.tm."""

from __future__ import annotations

import gzip
import json
import os
import re
import sys
import uuid
import urllib.error
import urllib.request
from typing import Any

from chapar_env import base_url, env


class AuthFatalError(RuntimeError):
    """Auth провалился — монитор остановлен."""


BASE = "https://billing.post.gov.tm"


def auto_login_enabled() -> bool:
    return (env("CHAPAR_AUTO_LOGIN", "1") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def phone_configured() -> bool:
    return bool(normalize_phone(env("CHAPAR_PHONE")))


def token_configured() -> bool:
    return bool(env("CHAPAR_TOKEN"))


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("993"):
        return digits
    if len(digits) == 8:
        return f"993{digits}"
    return digits


def _decode_response(raw: bytes, encoding: str | None) -> bytes:
    if encoding and "gzip" in encoding.lower():
        return gzip.decompress(raw)
    return raw


def _app_headers(*, auth: str = "") -> dict[str, str]:
    headers = {
        "accept-encoding": "deflate, gzip",
        "accept-language": env("CHAPAR_LANGUAGE", "tm") or "tm",
        "content-type": "application/json",
        "device-uuid": env("CHAPAR_DEVICE_UUID") or "6658e008-53bc-4bbb-8fd4-8b1472a14241",
        "host": "billing.post.gov.tm",
        "idempotency-key": str(uuid.uuid4()),
        "platform": env("CHAPAR_PLATFORM", "iOS") or "iOS",
        "theme": env("CHAPAR_THEME", "light") or "light",
        "user-agent": env("CHAPAR_USER_AGENT", "Dart/3.10 (dart:io)") or "Dart/3.10 (dart:io)",
        "version": env("CHAPAR_APP_VERSION", "5.2.0") or "5.2.0",
    }
    if auth:
        token = auth if auth.lower().startswith("bearer ") else f"Bearer {auth}"
        headers["authorization"] = token
    return headers


def api_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    auth: str = "",
) -> tuple[int, dict[str, Any] | str]:
    url = f"{base_url()}/{path.lstrip('/')}"
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method.upper())
    for key, value in _app_headers(auth=auth).items():
        req.add_header(key, value)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = _decode_response(resp.read(), resp.headers.get("Content-Encoding"))
            text = payload.decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        payload = _decode_response(exc.read(), exc.headers.get("Content-Encoding"))
        text = payload.decode("utf-8", errors="replace")
        status = exc.code

    try:
        parsed: dict[str, Any] | str = json.loads(text)
    except json.JSONDecodeError:
        parsed = text
    return status, parsed


def _extract_token(body: dict[str, Any] | str) -> str:
    if not isinstance(body, dict):
        raise RuntimeError(f"login: не JSON-объект: {body!r}")

    for key in ("token", "access_token", "accessToken"):
        val = body.get(key)
        if val:
            return str(val).strip()

    data = body.get("data")
    if isinstance(data, dict):
        for key in ("token", "access_token", "accessToken"):
            val = data.get(key)
            if val:
                return str(val).strip()

    raise RuntimeError(f"login: нет token в ответе: {body!r}")


def _apply_token(token: str) -> None:
    os.environ["CHAPAR_TOKEN"] = token


def request_otp(*, phone: str | None = None, verbose: bool = True) -> None:
    """POST /api/users/auth/otp — SMS-код на телефон."""
    normalized = normalize_phone(phone or env("CHAPAR_PHONE"))
    if not normalized:
        raise RuntimeError("CHAPAR_PHONE не задан")

    otp_type = env("CHAPAR_OTP_TYPE", "login") or "login"
    status, body = api_request(
        "POST",
        "api/users/auth/otp",
        body={"phone": normalized, "type": otp_type},
    )
    if status != 200:
        raise RuntimeError(f"otp → HTTP {status}: {body}")

    if verbose:
        print(f"[chapar-auth] OTP отправлен на {normalized} (type={otp_type})")


def _read_otp() -> str:
    preset = env("CHAPAR_OTP")
    if preset:
        return re.sub(r"\D", "", preset)

    if sys.stdin.isatty():
        try:
            raw = input("Введи OTP из SMS: ").strip()
        except EOFError as exc:
            raise RuntimeError("Нет OTP (задай CHAPAR_OTP или введи в терминале)") from exc
        code = re.sub(r"\D", "", raw)
        if code:
            return code

    raise RuntimeError("Нет OTP — задай CHAPAR_OTP в env или запусти в терминале")


def login_with_otp(
    *,
    phone: str | None = None,
    otp: str | None = None,
    request_code: bool = True,
    verbose: bool = True,
) -> str:
    """POST otp → POST login → token в памяти."""
    normalized = normalize_phone(phone or env("CHAPAR_PHONE"))
    if not normalized:
        raise RuntimeError("CHAPAR_PHONE не задан")

    if request_code:
        request_otp(phone=normalized, verbose=verbose)

    code = re.sub(r"\D", "", otp or _read_otp())
    if not code:
        raise RuntimeError("OTP пуст")

    status, body = api_request(
        "POST",
        "api/users/auth/login",
        body={"phone": normalized, "otp": code},
    )
    if status != 200:
        raise RuntimeError(f"login → HTTP {status}: {body}")

    token = _extract_token(body)
    _apply_token(token)
    if verbose:
        print(f"[chapar-auth] token: {token[:12]}…")
        print("[chapar-auth] токен в памяти")
    return token


def ensure_session(*, verbose: bool = True, force: bool = False) -> bool:
    if not auto_login_enabled():
        return False
    if token_configured() and not force:
        if verbose:
            print(f"[chapar-auth] token уже есть: {env('CHAPAR_TOKEN')[:12]}…")
        return True
    if not phone_configured():
        return False
    login_with_otp(verbose=verbose)
    return True


def build_headers() -> dict[str, str]:
    token = env("CHAPAR_TOKEN")
    if not token:
        raise SystemExit("Нет CHAPAR_TOKEN — запусти login (CHAPAR_AUTO_LOGIN=1)")
    return _app_headers(auth=token)
