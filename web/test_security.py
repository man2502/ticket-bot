"""Тесты паролей и cookie сессии.

Пароли здесь — единственная преграда между интернетом и кнопкой «Забронировать»,
поэтому проверяется не только «правильный пароль подходит», но и то, что
испорченная строка в базе трактуется как несовпадение, а не как ошибка или,
хуже, как совпадение.

Запуск (из корня репозитория):
    python run_tests.py web
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from web.security import (
    hash_password,
    make_session_token,
    read_session_token,
    verify_password,
)

PASSWORD = "правильный-пароль-42"


# --------------------------------------------------------------------------
# Хеширование
# --------------------------------------------------------------------------


def test_correct_password_matches() -> None:
    assert verify_password(PASSWORD, hash_password(PASSWORD)) is True


def test_wrong_password_does_not_match() -> None:
    assert verify_password("другой", hash_password(PASSWORD)) is False


def test_same_password_gives_different_hashes() -> None:
    """Соль случайная: одинаковые пароли не должны выглядеть одинаково в базе."""
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_hash_does_not_contain_password() -> None:
    assert PASSWORD not in hash_password(PASSWORD)


def test_hash_format_carries_parameters() -> None:
    """Параметры лежат в строке — их можно поднять, не ломая старые пароли."""
    parts = hash_password(PASSWORD).split("$")
    assert parts[0] == "scrypt"
    assert len(parts) == 6
    assert int(parts[1]) >= 2**14  # n


def test_empty_password_is_rejected() -> None:
    with pytest.raises(ValueError):
        hash_password("")


def test_empty_password_never_verifies() -> None:
    assert verify_password("", hash_password(PASSWORD)) is False


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "мусор",
        "scrypt$",
        "scrypt$нечисло$8$1$aa$bb",
        "bcrypt$32768$8$1$aa$bb",  # чужой алгоритм
        "scrypt$32768$8$1$zz$bb",  # соль не hex
    ],
)
def test_broken_hash_is_a_mismatch(stored: str) -> None:
    """Битая строка в базе — это «не совпало», а не исключение."""
    assert verify_password(PASSWORD, stored) is False


def test_unicode_password_survives_roundtrip() -> None:
    secret = "Пароль-Ñ-日本-🔑"
    assert verify_password(secret, hash_password(secret)) is True


def test_long_password_is_supported() -> None:
    """У scrypt нет ограничения в 72 байта, как у bcrypt."""
    secret = "д" * 200
    stored = hash_password(secret)
    assert verify_password(secret, stored) is True
    assert verify_password("д" * 199, stored) is False


# --------------------------------------------------------------------------
# Cookie сессии
# --------------------------------------------------------------------------


def test_session_roundtrip() -> None:
    assert read_session_token(make_session_token(42)) == 42


def test_tampered_token_is_rejected() -> None:
    """Подпись обязана ломаться при правке содержимого — иначе можно стать чужим."""
    token = make_session_token(1)
    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    assert read_session_token(tampered) is None


def test_empty_token_is_none() -> None:
    assert read_session_token("") is None


def test_garbage_token_is_none() -> None:
    assert read_session_token("совсем-не-токен") is None
