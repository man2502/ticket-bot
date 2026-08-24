"""Тесты разметки капч.

Два опасных места. Первое — имя файла приходит из адреса страницы, и без
проверки через него читается любой файл с диска, включая ключ шифрования.
Второе — разметка перекладывает файлы в обучающую выборку, и ошибка здесь
портит датасет молча.

Запуск (из корня репозитория):
    python run_tests.py web/test_captcha.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# captcha_dataset_api живёт в модуле бота — он не пакет, импортируется по пути.
for _path in (REPO_ROOT, REPO_ROOT / 'airlines_api'):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# Минимальный валидный PNG 1×1 — содержимое неважно, важны имя и перемещение.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


@pytest.fixture()
def dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Датасет во временной папке: рабочий трогать нельзя."""
    import captcha_dataset_api as data

    root = tmp_path / "captcha_dataset_api"
    folders = {
        "DATASET_DIR": root,
        "CONFIRMED_DIR": root / "confirmed",
        "HARD_DIR": root / "hard",
        "REJECTED_DIR": root / "rejected",
        "UNLABELED_DIR": root / "unlabeled",
    }
    for name, path in folders.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(data, name, path)

    return folders


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import web.config as config
    import web.crypto as crypto
    import web.db as db_module

    monkeypatch.setenv("TICKET_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(crypto, "SECRET_KEY_FILE", tmp_path / "secret.key")
    monkeypatch.setattr(crypto, "SESSION_KEY_FILE", tmp_path / "session.key")
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_SessionFactory", None)

    db_module.init_db()
    with db_module.session_scope() as session:
        yield session


def _rejected(dataset, wrong_code: str = "12345", reason: str = "invalid") -> str:
    """Положить картинку так, как это делает бот при отвергнутой капче."""
    name = f"{wrong_code}_cnn_{reason}_abcdef123456.png"
    (dataset["REJECTED_DIR"] / name).write_bytes(PNG)
    return name


def _unlabeled(dataset, tag: str = "aabbccddeeff") -> str:
    name = f"unlabeled_{tag}.png"
    (dataset["UNLABELED_DIR"] / name).write_bytes(PNG)
    return name


# --------------------------------------------------------------------------
# Защита имени файла
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../../../web/data/secret.key",
        "..\\..\\secret.key",
        "/etc/passwd",
        ".hidden",
        "sub/dir.png",
    ],
)
def test_path_traversal_is_blocked(dataset, name: str) -> None:
    """Имя приходит из адреса: без проверки через него читается любой файл."""
    from web.services.captcha import CaptchaError, sample_path

    with pytest.raises(CaptchaError):
        sample_path(name)


def test_unknown_file_is_rejected(dataset) -> None:
    from web.services.captcha import CaptchaError, sample_path

    with pytest.raises(CaptchaError):
        sample_path("нет-такого.png")


def test_existing_file_is_found(dataset) -> None:
    from web.services.captcha import sample_path

    name = _rejected(dataset)
    assert sample_path(name).name == name


# --------------------------------------------------------------------------
# Список на разметку
# --------------------------------------------------------------------------


def test_rejected_shows_cnn_variant(dataset) -> None:
    from web.services.captcha import list_pending

    _rejected(dataset, wrong_code="54321", reason="invalid")
    item = list_pending()[0]

    assert item.kind == "rejected"
    assert item.cnn_guess == "54321"
    assert "54321" in item.hint


def test_unlabeled_has_no_guess(dataset) -> None:
    from web.services.captcha import list_pending

    _unlabeled(dataset)
    item = list_pending()[0]

    assert item.kind == "unlabeled"
    assert item.cnn_guess == ""
    assert "неизвестен" in item.hint


def test_errors_of_model_come_first(dataset) -> None:
    """Они полезнее для обучения: модель учится на своих ошибках."""
    from web.services.captcha import list_pending

    _unlabeled(dataset)
    _rejected(dataset)

    assert list_pending()[0].kind == "rejected"


def test_empty_dataset_is_not_an_error(dataset) -> None:
    from web.services.captcha import list_pending, stats

    assert list_pending() == []
    assert stats()["rejected"] == 0


def test_stats_count_everything(dataset) -> None:
    from web.services.captcha import stats

    _rejected(dataset, wrong_code="11111")
    _rejected(dataset, wrong_code="22222")
    _unlabeled(dataset)

    counters = stats()
    assert counters["rejected"] == 2
    assert counters["unlabeled"] == 1


# --------------------------------------------------------------------------
# Разметка
# --------------------------------------------------------------------------


def test_labeling_moves_file_to_hard(dataset, db) -> None:
    from web.services.captcha import label_sample

    name = _rejected(dataset)
    label_sample(db, name, "98765")

    assert not (dataset["REJECTED_DIR"] / name).exists()
    assert list(dataset["HARD_DIR"].glob("*.png"))


def test_labeled_code_is_in_filename(dataset, db) -> None:
    """Обучение читает код из имени файла — если он не тот, датасет испорчен."""
    from web.services.captcha import label_sample

    label_sample(db, _rejected(dataset), "98765")
    hard = list(dataset["HARD_DIR"].glob("*.png"))[0]

    assert hard.name.startswith("98765")


def test_unlabeled_can_be_labeled_too(dataset, db) -> None:
    from web.services.captcha import label_sample

    name = _unlabeled(dataset)
    label_sample(db, name, "13579")

    assert not (dataset["UNLABELED_DIR"] / name).exists()
    assert list(dataset["HARD_DIR"].glob("13579*.png"))


@pytest.mark.parametrize("code", ["123", "1234567", "", "abcde"])
def test_wrong_length_is_rejected(dataset, db, code: str) -> None:
    """Пять цифр — иначе в датасет уедет мусор."""
    from web.services.captcha import CaptchaError, label_sample

    name = _rejected(dataset)
    with pytest.raises(CaptchaError):
        label_sample(db, name, code)

    # Файл должен остаться на месте: разметка не состоялась.
    assert (dataset["REJECTED_DIR"] / name).exists()


def test_spaces_and_dashes_are_stripped(dataset, db) -> None:
    """Люди вводят с пробелами — это не повод отказывать."""
    from web.services.captcha import label_sample

    label_sample(db, _rejected(dataset), "9 87-65")
    assert list(dataset["HARD_DIR"].glob("98765*.png"))


def test_labeling_is_journalled(dataset, db) -> None:
    from web.models import CaptchaSample
    from web.services.captcha import label_sample

    label_sample(db, _rejected(dataset, wrong_code="11111"), "98765", user_id=None)

    row = db.query(CaptchaSample).one()
    assert row.correct_code == "98765"
    assert row.cnn_guess == "11111"
    assert row.status == "labeled"


def test_labeling_missing_file_is_reported(dataset, db) -> None:
    from web.services.captcha import CaptchaError, label_sample

    with pytest.raises(CaptchaError):
        label_sample(db, "12345_cnn_invalid_000000000000.png", "98765")


# --------------------------------------------------------------------------
# Удаление нечитаемых
# --------------------------------------------------------------------------


def test_discard_removes_file(dataset, db) -> None:
    from web.services.captcha import discard_sample

    name = _rejected(dataset)
    discard_sample(db, name)

    assert not (dataset["REJECTED_DIR"] / name).exists()
    assert not list(dataset["HARD_DIR"].glob("*.png"))


def test_discard_is_journalled(dataset, db) -> None:
    """Видно, что картинку не потеряли, а сознательно убрали."""
    from web.models import CaptchaSample
    from web.services.captcha import discard_sample

    discard_sample(db, _rejected(dataset))
    assert db.query(CaptchaSample).one().status == "discarded"


def test_discard_does_not_pollute_training_set(dataset, db) -> None:
    from web.services.captcha import discard_sample, stats

    discard_sample(db, _rejected(dataset))
    assert stats()["hard"] == 0


@pytest.mark.parametrize(
    "reason",
    ["invalid", "mismatch_manual", "server_mismatch"],
)
def test_all_bot_reasons_are_parsed(dataset, reason: str) -> None:
    """
    Все причины, с которыми бот сохраняет отвергнутую капчу, должны разбираться.

    Две из трёх содержат подчёркивание, и строгий разбор терял их молча:
    разметка показывала «код неизвестен» там, где вариант модели был известен.
    """
    from web.services.captcha import list_pending

    name = _rejected(dataset, wrong_code="54321", reason=reason)
    item = next(i for i in list_pending() if i.name == name)

    assert item.cnn_guess == "54321"
    assert item.reason == reason


def test_hint_explains_each_reason(dataset) -> None:
    """Человеку нужно понимать, почему картинка тут оказалась."""
    from web.services.captcha import list_pending

    _rejected(dataset, wrong_code="11111", reason="mismatch_manual")
    hint = list_pending()[0].hint

    assert "11111" in hint
    assert "ручным вводом" in hint
