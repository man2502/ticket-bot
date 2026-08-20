"""Пароли и сессии.

Хеширование — `hashlib.scrypt` из стандартной библиотеки. Отдельный пакет
(bcrypt/argon2) не нужен: scrypt устойчив к перебору на видеокартах, входит
в Python с версии 3.6 и не тянет за собой сборку C-расширения на Windows.

Формат строки в базе:  scrypt$n$r$p$<соль hex>$<хеш hex>
Параметры лежат прямо в строке, поэтому их можно поднять в будущем, не ломая
уже сохранённые пароли: проверка читает те параметры, с которыми хеш создан.
"""

from __future__ import annotations

import hashlib
import hmac
import os

__all__ = [
    "hash_password",
    "verify_password",
    "make_session_token",
    "read_session_token",
    "SESSION_COOKIE",
    "SESSION_MAX_AGE_SEC",
]

SESSION_COOKIE = "ticket_session"
SESSION_MAX_AGE_SEC = 14 * 24 * 3600  # две недели

# Параметры scrypt. n=2**15 — около 100 мс на проверку: незаметно человеку
# и дорого перебору. r и p — рекомендованные значения.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_LEN = 32


def _maxmem(n: int, r: int, p: int) -> int:
    """
    Лимит памяти для scrypt.

    OpenSSL по умолчанию разрешает ровно 32 МиБ, а n=2**15, r=8 требует
    128 * n * r = как раз 32 МиБ — и вызов падает с «memory limit exceeded».
    Считаем требуемый объём по формуле и берём его с запасом, иначе выбор
    параметров молча ограничен снизу настройками OpenSSL.
    """
    required = 128 * n * r * max(1, p)
    return required * 2


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("Пустой пароль")
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_LEN,
        maxmem=_maxmem(_SCRYPT_N, _SCRYPT_R, _SCRYPT_P),
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """
    Проверить пароль. Любая непонятная строка в базе — это «не совпало».

    Сравнение через `hmac.compare_digest`: обычное `==` завершается на первом
    несовпавшем байте, и по времени ответа хеш можно подбирать посимвольно.
    """
    if not password or not stored:
        return False

    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False

    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except ValueError:
        return False

    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            # Параметры берутся из самой строки, поэтому и лимит считаем по ним:
            # хеш мог быть создан с другими n и r.
            maxmem=_maxmem(n, r, p),
        )
    except ValueError:
        # Некорректные параметры в строке (n не степень двойки и т.п.).
        return False

    return hmac.compare_digest(digest, expected)


def _serializer():
    """
    Сериализатор подписанной cookie.

    Импорт ленивый намеренно: хеширование паролей работает на одной стандартной
    библиотеке, и требовать `itsdangerous` ради него — значит не дать проверить
    вход там, где пакеты ещё не поставлены.
    """
    from itsdangerous import URLSafeTimedSerializer  # noqa: PLC0415

    from web.crypto import session_secret  # noqa: PLC0415

    return URLSafeTimedSerializer(session_secret(), salt="ticket-session")


def make_session_token(user_id: int) -> str:
    """Подписанная cookie. Содержимое не шифруется — там только id пользователя."""
    return _serializer().dumps({"uid": user_id})


def read_session_token(token: str) -> int | None:
    """id пользователя из cookie или None, если подпись не сошлась или истекла."""
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE_SEC)
    except Exception:  # noqa: BLE001 — битая, чужая или истёкшая подпись
        return None
    if not isinstance(data, dict):
        return None
    uid = data.get("uid")
    return uid if isinstance(uid, int) else None
