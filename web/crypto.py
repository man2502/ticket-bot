"""Шифрование секретов, которые лежат в базе.

Что шифруется: пароли T5 и Railway, пароль SMTP, ключ SMS-шлюза. То есть всё,
чем можно воспользоваться, унеся файл `ticket.db`.

Ключ хранится отдельным файлом (`data/secret.key`), а не в базе — иначе
шифрование не имело бы смысла. Файл создаётся автоматически при первом запуске
и получает права 600 там, где ОС это поддерживает.

Ключ можно задать переменной `TICKET_SECRET_KEY` — так удобнее в докере и на
сервере, где файловая система общая.

ВАЖНО: потеря ключа означает потерю всех сохранённых паролей. Их придётся
ввести заново через веб. Сама база при этом не ломается — расшифровка вернёт
понятную ошибку, а не мусор.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from web.config import SECRET_KEY_FILE, SESSION_KEY_FILE, ensure_dirs

__all__ = ["encrypt", "decrypt", "session_secret", "SecretUnreadable"]


class SecretUnreadable(RuntimeError):
    """Значение в базе не расшифровывается текущим ключом."""


def _write_key_file(path: Path, value: bytes) -> None:
    ensure_dirs()
    path.write_bytes(value)
    try:
        # Ключ читает только владелец. На Windows chmod почти ничего не делает,
        # но и вреда не наносит — там защита обеспечивается ACL профиля.
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _load_or_create(path: Path, env_name: str, generator) -> bytes:
    from_env = (os.getenv(env_name) or "").strip()
    if from_env:
        return from_env.encode("utf-8")

    if path.exists():
        value = path.read_bytes().strip()
        if value:
            return value

    value = generator()
    _write_key_file(path, value)
    print(f"[crypto] создан новый ключ: {path}")
    return value


def _fernet() -> Fernet:
    key = _load_or_create(SECRET_KEY_FILE, "TICKET_SECRET_KEY", Fernet.generate_key)
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise SecretUnreadable(
            f"Ключ шифрования испорчен ({SECRET_KEY_FILE}): {exc}. "
            f"Удали файл — он создастся заново, но сохранённые пароли придётся ввести заново."
        ) from exc


def encrypt(value: str) -> str:
    """Зашифровать значение для хранения в базе. Пустая строка остаётся пустой."""
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    """
    Расшифровать значение из базы.

    Ошибку не глотаем: молча вернуть пустую строку значило бы запустить бота
    с пустым паролем и получить непонятную ошибку авторизации на сайте T5.
    """
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretUnreadable(
            "Значение зашифровано другим ключом — задай его заново через веб-панель"
        ) from exc


def session_secret() -> bytes:
    """Ключ подписи cookie сессии. Смена ключа разлогинивает всех."""
    return _load_or_create(
        SESSION_KEY_FILE, "TICKET_SESSION_KEY", lambda: os.urandom(32).hex().encode("ascii")
    )
