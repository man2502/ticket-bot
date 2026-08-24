"""Тесты проверки данных пассажира.

Ошибка в этих полях обнаруживается не при сохранении, а на странице оплаты —
когда место уже занято, а бронь не проходит. Поэтому проверок много и они
придирчивые.

Запуск (из корня репозитория):
    python run_tests.py web/test_passengers.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from web.services.passengers import (
    clean_passenger_form,
    export_for_bot,
    format_dob,
    normalize_phone,
    pax_type_on_date,
    validate_group,
)

VALID = {
    "first_name": "Anvar",
    "last_name": "Test",
    "dob": "01.01.1990",
    "gender": "M",
    "doc_number": "AA1234567",
    "citizenship": "TM",
}


def _form(**over):
    data = dict(VALID)
    data.update(over)
    return clean_passenger_form(data)


# --------------------------------------------------------------------------
# Общее
# --------------------------------------------------------------------------


def test_valid_form_passes() -> None:
    assert _form().is_valid is True


def test_all_errors_reported_at_once() -> None:
    """Человек должен увидеть весь список, а не исправлять по одному за раз."""
    form = _form(first_name="", dob="абракадабра", doc_number="!")
    assert set(form.errors) == {"first_name", "dob", "doc_number"}


# --------------------------------------------------------------------------
# Имя и фамилия
# --------------------------------------------------------------------------


def test_name_is_uppercased() -> None:
    """В паспорте латиница заглавными — так же отправляем перевозчику."""
    assert _form(first_name="anvar", last_name="test").first_name == "ANVAR"


def test_cyrillic_name_is_rejected() -> None:
    """Кириллица уходит на сайт как есть и ломает бронь."""
    form = _form(first_name="Анвар")
    assert "first_name" in form.errors


def test_double_name_is_allowed() -> None:
    assert _form(last_name="Van Der Berg").is_valid is True


def test_hyphenated_name_is_allowed() -> None:
    assert _form(first_name="Anna-Maria").is_valid is True


def test_extra_spaces_are_collapsed() -> None:
    assert _form(last_name="  Van   Der  Berg  ").last_name == "VAN DER BERG"


def test_empty_name_is_rejected() -> None:
    assert "last_name" in _form(last_name="   ").errors


def test_digits_in_name_are_rejected() -> None:
    assert "first_name" in _form(first_name="Anvar2").errors


# --------------------------------------------------------------------------
# Дата рождения
# --------------------------------------------------------------------------


def test_dob_is_normalised_to_dotted_format() -> None:
    """Боты ждут ДД.ММ.ГГГГ — приводим независимо от того, как ввели."""
    assert _form(dob="1990-01-05").dob == "05.01.1990"
    assert _form(dob="5.1.1990").dob == "05.01.1990"


def test_future_dob_is_rejected() -> None:
    future = dt.date.today() + dt.timedelta(days=1)
    form = _form(dob=f"{future.day:02d}.{future.month:02d}.{future.year}")
    assert "dob" in form.errors


def test_absurd_age_is_rejected() -> None:
    assert "dob" in _form(dob="01.01.1800").errors


def test_garbage_dob_is_rejected() -> None:
    assert "dob" in _form(dob="вчера").errors


def test_empty_dob_is_rejected() -> None:
    assert "dob" in _form(dob="").errors


def test_format_dob_pads_single_digits() -> None:
    assert format_dob("5.1.1990") == "05.01.1990"


# --------------------------------------------------------------------------
# Тип пассажира
# --------------------------------------------------------------------------


def test_type_is_counted_on_flight_date_not_today() -> None:
    """
    Ребёнку исполняется 18 до вылета — летит по взрослому тарифу.

    Считать «на сегодня» значит забронировать детский тариф и получить отказ
    при посадке.
    """
    birthday = dt.date(2008, 6, 1)
    dob = f"{birthday.day:02d}.{birthday.month:02d}.{birthday.year}"

    assert pax_type_on_date(dob, dt.date(2026, 5, 31)) == "child"
    assert pax_type_on_date(dob, dt.date(2026, 6, 1)) == "adult"


def test_infant_boundary_is_five_years() -> None:
    dob = "01.06.2021"
    assert pax_type_on_date(dob, dt.date(2026, 5, 31)) == "infant"
    assert pax_type_on_date(dob, dt.date(2026, 6, 1)) == "child"


def test_explicit_type_is_kept() -> None:
    assert _form(pax_type="child").pax_type == "child"


def test_empty_type_means_auto() -> None:
    assert _form(pax_type="").pax_type == ""


def test_unknown_type_is_rejected() -> None:
    assert "pax_type" in _form(pax_type="студент").errors


# --------------------------------------------------------------------------
# Документ
# --------------------------------------------------------------------------


def test_doc_number_keeps_separators() -> None:
    """
    Туркменский паспорт — «I-LB 897989», и уходит он перевозчику именно так.

    Прежняя версия вырезала дефис и пробел, молча превращая номер
    в «ILB897989»: бронь ушла бы с неверным документом, а выяснилось бы
    это на регистрации.
    """
    assert _form(doc_number="I-LB 897989").doc_number == "I-LB 897989"


def test_doc_number_is_uppercased() -> None:
    assert _form(doc_number="i-lb 897989").doc_number == "I-LB 897989"


def test_doc_number_extra_spaces_collapsed() -> None:
    """Лишние пробелы убираем, но сам разделитель остаётся."""
    assert _form(doc_number="  I-LB   897989  ").doc_number == "I-LB 897989"


def test_plain_doc_number_still_works() -> None:
    assert _form(doc_number="AA1234567").doc_number == "AA1234567"


def test_short_doc_number_is_rejected() -> None:
    assert "doc_number" in _form(doc_number="AA1").errors


def test_doc_number_with_symbols_is_rejected() -> None:
    assert "doc_number" in _form(doc_number="AA123/456").errors


def test_doc_number_without_digits_is_rejected() -> None:
    """Номер без единой цифры — почти наверняка в поле попало не то."""
    assert "doc_number" in _form(doc_number="PASSPORT").errors


def test_normalized_form_matches_the_same_document() -> None:
    """«I-LB 897989» и «ILB897989» — один паспорт, записанный по-разному."""
    from web.services.passengers import normalize_doc_number

    assert normalize_doc_number("I-LB 897989") == normalize_doc_number("ilb897989")


def test_empty_doc_number_is_rejected() -> None:
    assert "doc_number" in _form(doc_number="").errors


def test_doc_expiry_is_optional() -> None:
    assert _form(doc_expdt="").is_valid is True


def test_expired_document_is_flagged() -> None:
    """Чаще всего это опечатка в годе; узнать о ней на регистрации — поздно."""
    assert "doc_expdt" in _form(doc_expdt="01.01.2020").errors


def test_valid_future_expiry_passes() -> None:
    future = dt.date.today() + dt.timedelta(days=365)
    form = _form(doc_expdt=f"{future.day:02d}.{future.month:02d}.{future.year}")
    assert form.is_valid is True


# --------------------------------------------------------------------------
# Телефон
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["61234567", "+99361234567", "99361234567", "+993 61 23 45 67", "861234567"],
)
def test_phone_forms_normalise_to_one(raw: str) -> None:
    assert normalize_phone(raw) == "+99361234567"


def test_phone_is_optional() -> None:
    assert _form(phone="").is_valid is True


def test_short_phone_is_rejected() -> None:
    assert "phone" in _form(phone="123").errors


def test_empty_phone_normalises_to_empty() -> None:
    assert normalize_phone("") == ""


# --------------------------------------------------------------------------
# Прочее
# --------------------------------------------------------------------------


def test_email_is_lowercased() -> None:
    assert _form(email="Anvar@Example.TM").email == "anvar@example.tm"


def test_email_is_optional() -> None:
    assert _form(email="").is_valid is True


def test_broken_email_is_rejected() -> None:
    assert "email" in _form(email="anvar@").errors


def test_gender_is_uppercased() -> None:
    assert _form(gender="f").gender == "F"


def test_unknown_gender_is_rejected() -> None:
    assert "gender" in _form(gender="X").errors


def test_citizenship_default_and_case() -> None:
    assert _form(citizenship="tm").citizenship == "TM"


def test_bad_citizenship_is_rejected() -> None:
    assert "citizenship" in _form(citizenship="Туркменистан").errors


def test_note_is_truncated() -> None:
    assert len(_form(note="я" * 400).note) == 255


# --------------------------------------------------------------------------
# Состав группы и передача ботам
# --------------------------------------------------------------------------


def _pax(dob: str, pax_type: str = "", **over) -> dict:
    data = {
        "id": over.get("id", 1),
        "first_name": "A",
        "last_name": "B",
        "dob": dob,
        "doc_number": "AA1234567",
    }
    if pax_type:
        data["type"] = pax_type
    data.update(over)
    return data


FLIGHT = "10.08.2026"


def test_group_needs_at_least_one_passenger() -> None:
    assert validate_group([], FLIGHT)


def test_adult_alone_is_fine() -> None:
    assert validate_group([_pax("01.01.1990")], FLIGHT) == []


def test_child_without_adult_is_rejected() -> None:
    """Перевозчик не оформит ребёнка без сопровождающего."""
    errors = validate_group([_pax("01.01.2015")], FLIGHT)
    assert any("взрослый" in e for e in errors)


def test_child_with_adult_is_fine() -> None:
    group = [_pax("01.01.1990", id=1), _pax("01.01.2015", id=2)]
    assert validate_group(group, FLIGHT) == []


def test_more_infants_than_adults_is_rejected() -> None:
    """Младенец летит на руках — на каждого нужен свой взрослый."""
    group = [
        _pax("01.01.1990", id=1),
        _pax("01.01.2024", id=2),
        _pax("01.01.2025", id=3),
    ]
    errors = validate_group(group, FLIGHT)
    assert any("Младенцев" in e for e in errors)


def test_group_type_is_counted_on_flight_date() -> None:
    """
    На дату рейса пассажиру уже 18 — значит взрослый, и ребёнок с ним поедет.

    Если бы тип считался на сегодня, состав признали бы недопустимым.
    """
    group = [_pax("01.07.2008", id=1), _pax("01.01.2015", id=2)]
    assert validate_group(group, "01.08.2026") == []


def test_explicit_type_overrides_age() -> None:
    group = [_pax("01.01.1990", pax_type="adult", id=1), _pax("01.01.2015", "child", id=2)]
    assert validate_group(group, FLIGHT) == []


def test_broken_flight_date_is_reported() -> None:
    errors = validate_group([_pax("01.01.1990")], "когда-нибудь")
    assert any("Дата рейса" in e for e in errors)


def test_broken_dob_in_group_is_reported() -> None:
    errors = validate_group([_pax("не дата")], FLIGHT)
    assert any("дату рождения" in e for e in errors)


def test_export_writes_file_and_ids(tmp_path: Path) -> None:
    """Файл должен читаться тем же загрузчиком, которым пользуются боты."""
    import os

    from passenger_loader import load_passengers_list

    group = [_pax("01.01.1990", id=7), _pax("01.01.2015", id=9)]
    path, ids = export_for_bot(group, tmp_path)

    assert ids == "7,9"
    assert path.name == "passengers.json"

    os.environ["PASSENGERS_FILE"] = str(path)
    try:
        loaded = load_passengers_list(path)
    finally:
        os.environ.pop("PASSENGERS_FILE", None)

    assert [p["id"] for p in loaded] == [7, 9]


def test_export_preserves_order(tmp_path: Path) -> None:
    """Порядок влияет на распределение мест — менять его нельзя."""
    group = [_pax("01.01.1990", id=9), _pax("01.01.1991", id=7)]
    _path, ids = export_for_bot(group, tmp_path)
    assert ids == "9,7"
