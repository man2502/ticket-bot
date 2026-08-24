"""Проверка и нормализация данных пассажира.

Модуль намеренно не знает ни про FastAPI, ни про ORM: сюда приходят строки из
формы, отсюда уходят проверенные значения или список ошибок. Так валидацию
можно прогнать тестами без поднятия приложения — а она здесь самая дорогая
часть: опечатка в номере паспорта обнаруживается уже на странице оплаты,
когда место занято, а бронь не проходит.

Разбор даты рождения и определение типа пассажира взяты из `passenger_loader` —
того самого модуля, который читают боты. Иначе веб принимал бы данные, которые
бот потом не разберёт.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from passenger_loader import age_on_date, infer_pax_type_from_dob, parse_dob  # noqa: E402

__all__ = [
    "PassengerForm",
    "clean_passenger_form",
    "export_for_bot",
    "validate_group",
    "normalize_phone",
    "normalize_doc_number",
    "pax_type_on_date",
    "format_dob",
    "GENDERS",
    "PAX_TYPES",
]

GENDERS = (("M", "мужской"), ("F", "женский"))

# Пустое значение = определять по дате рождения на дату рейса.
PAX_TYPES = (
    ("", "по дате рождения"),
    ("adult", "взрослый"),
    ("child", "ребёнок"),
    ("infant", "младенец"),
)

_VALID_PAX_TYPES = {value for value, _ in PAX_TYPES}

# Латиница обязательна: в паспортах и в формах перевозчиков имя пишется
# латиницей, а кириллица уходит на сайт как есть и ломает бронь.
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z\s'\-]{0,63}$")

# Номер документа: буквы, цифры, дефисы и пробелы.
#
# Разделители ОБЯЗАТЕЛЬНО сохраняются. Туркменский паспорт выглядит как
# «I-LB 897989», и именно в таком виде номер уходит в CreateBooking. Прежняя
# версия вырезала пробелы и дефисы, молча превращая номер в «ILB897989», —
# бронь ушла бы с неверным документом, а выяснилось бы это на регистрации.
_DOC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{3,29}$")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Разумные границы даты рождения: 120 лет назад и не в будущем.
_MAX_AGE_YEARS = 120


@dataclass
class PassengerForm:
    """Проверенные значения формы плюс накопленные ошибки по полям."""

    first_name: str = ""
    last_name: str = ""
    dob: str = ""
    gender: str = "M"
    doc_number: str = ""
    doc_expdt: str = ""
    citizenship: str = "TM"
    email: str = ""
    phone: str = ""
    pax_type: str = ""
    note: str = ""
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, str]:
        """Поля без ошибок — для записи в модель."""
        return {
            "first_name": self.first_name,
            "last_name": self.last_name,
            "dob": self.dob,
            "gender": self.gender,
            "doc_number": self.doc_number,
            "doc_expdt": self.doc_expdt,
            "citizenship": self.citizenship,
            "email": self.email,
            "phone": self.phone,
            "pax_type": self.pax_type,
            "note": self.note,
        }


def format_dob(value: str) -> str:
    """Привести дату к ДД.ММ.ГГГГ — формату, который ждут оба бота."""
    parsed = parse_dob(value)
    return f"{parsed.day:02d}.{parsed.month:02d}.{parsed.year}"


def normalize_phone(raw: str) -> str:
    """
    Туркменский номер к виду +993XXXXXXXX.

    Люди пишут по-разному: 61234567, 861234567, +993 61 23 45 67, 99361234567.
    Приводим к одному виду здесь, чтобы SMS уходила по номеру, а не по тому,
    что человек успел набрать.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("993") and len(digits) > 8:
        digits = digits[3:]
    # Ведущая восьмёрка — привычка со старой междугородней нумерации.
    if digits.startswith("8") and len(digits) == 9:
        digits = digits[1:]
    return f"+993{digits}"


def pax_type_on_date(dob: str, on_date: dt.date | None = None) -> str:
    """
    Тип пассажира на дату рейса: infant 0-4, child 5-17, adult 18+.

    Считать надо именно на дату рейса, а не на сегодня: ребёнок, которому
    восемнадцать исполняется до вылета, летит по взрослому тарифу.
    """
    return infer_pax_type_from_dob(dob, on_date=on_date or dt.date.today())


def _clean_name(value: str, field_name: str, label: str, errors: dict[str, str]) -> str:
    text = " ".join((value or "").split())
    if not text:
        errors[field_name] = f"{label}: не заполнено"
        return ""
    if not _NAME_RE.match(text):
        errors[field_name] = f"{label}: только латиница, как в паспорте"
        return text
    return text.upper()


def _clean_dob(value: str, errors: dict[str, str]) -> str:
    text = (value or "").strip()
    if not text:
        errors["dob"] = "Дата рождения: не заполнена"
        return ""

    try:
        parsed = parse_dob(text)
    except ValueError:
        errors["dob"] = "Дата рождения: нужен формат ДД.ММ.ГГГГ"
        return text

    today = dt.date.today()
    if parsed > today:
        errors["dob"] = "Дата рождения: в будущем"
        return text
    if age_on_date(parsed, today) > _MAX_AGE_YEARS:
        errors["dob"] = f"Дата рождения: больше {_MAX_AGE_YEARS} лет"
        return text

    return f"{parsed.day:02d}.{parsed.month:02d}.{parsed.year}"


def _clean_doc_expiry(value: str, errors: dict[str, str]) -> str:
    """
    Срок действия документа. Поле необязательное.

    Просроченный документ не отклоняем, а показываем как ошибку формы: чаще
    всего это опечатка в годе, и молча пропустить её значит узнать о проблеме
    на регистрации.
    """
    text = (value or "").strip()
    if not text:
        return ""

    try:
        parsed = parse_dob(text)
    except ValueError:
        errors["doc_expdt"] = "Срок документа: нужен формат ДД.ММ.ГГГГ"
        return text

    if parsed < dt.date.today():
        errors["doc_expdt"] = "Срок документа: уже истёк"
        return f"{parsed.day:02d}.{parsed.month:02d}.{parsed.year}"

    return f"{parsed.day:02d}.{parsed.month:02d}.{parsed.year}"


def normalize_doc_number(value: str) -> str:
    """
    Номер без разделителей — только для сравнения, не для отправки.

    Нужен, чтобы «I-LB 897989» и «ILB897989» опознавались как один документ
    и человек не завёл одного пассажира дважды.
    """
    return re.sub(r"[\s\-]", "", (value or "")).upper()


def _clean_doc_number(value: str, errors: dict[str, str]) -> str:
    # Пробелы схлопываем и обрезаем по краям, но НЕ вырезаем: разделители
    # входят в номер и должны уйти перевозчику как есть.
    text = " ".join((value or "").split()).upper()

    if not text:
        errors["doc_number"] = "Номер документа: не заполнен"
        return ""
    if not _DOC_RE.match(text):
        errors["doc_number"] = "Номер документа: 4-30 символов — буквы, цифры, дефис, пробел"
        return text
    if not any(ch.isdigit() for ch in text):
        # Номер без единой цифры — почти наверняка в поле попало не то.
        errors["doc_number"] = "Номер документа: должна быть хотя бы одна цифра"
    return text


def _clean_email(value: str, errors: dict[str, str]) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    if not _EMAIL_RE.match(text):
        errors["email"] = "Email: непохож на адрес"
    return text


def _clean_phone(value: str, errors: dict[str, str]) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    normalized = normalize_phone(text)
    # +993 плюс восемь цифр национального номера.
    if len(normalized) != 12:
        errors["phone"] = "Телефон: 8 цифр туркменского номера, напр. 61234567"
        return text
    return normalized


def clean_passenger_form(data: dict[str, str]) -> PassengerForm:
    """
    Проверить всю форму разом.

    Возвращает объект со всеми полями и словарём ошибок. Проверяются все поля,
    а не только первое неверное: человеку нужно видеть весь список сразу,
    а не исправлять по одному за перезагрузку.
    """
    errors: dict[str, str] = {}

    gender = (data.get("gender") or "M").strip().upper()
    if gender not in {value for value, _ in GENDERS}:
        errors["gender"] = "Пол: M или F"
        gender = "M"

    pax_type = (data.get("pax_type") or "").strip().lower()
    if pax_type not in _VALID_PAX_TYPES:
        errors["pax_type"] = "Тип пассажира: неизвестное значение"
        pax_type = ""

    citizenship = (data.get("citizenship") or "TM").strip().upper()
    if not re.match(r"^[A-Z]{2,3}$", citizenship):
        errors["citizenship"] = "Гражданство: код страны, напр. TM"
        citizenship = "TM"

    return PassengerForm(
        first_name=_clean_name(data.get("first_name", ""), "first_name", "Имя", errors),
        last_name=_clean_name(data.get("last_name", ""), "last_name", "Фамилия", errors),
        dob=_clean_dob(data.get("dob", ""), errors),
        gender=gender,
        doc_number=_clean_doc_number(data.get("doc_number", ""), errors),
        doc_expdt=_clean_doc_expiry(data.get("doc_expdt", ""), errors),
        citizenship=citizenship,
        email=_clean_email(data.get("email", ""), errors),
        phone=_clean_phone(data.get("phone", ""), errors),
        pax_type=pax_type,
        note=" ".join((data.get("note") or "").split())[:255],
        errors=errors,
    )


# --------------------------------------------------------------------------- #
# Передача пассажиров ботам
# --------------------------------------------------------------------------- #


def validate_group(passengers: list[dict], flight_date: str) -> list[str]:
    """
    Проверить состав группы на дату рейса. Возвращает список ошибок.

    Ловим здесь, а не при запуске бота: бот на таком составе просто упадёт
    с RuntimeError уже в цикле поиска, и человек увидит это в логах вместо
    внятного сообщения в форме.
    """
    if not passengers:
        return ["Не выбран ни один пассажир"]

    try:
        on_date = parse_dob(flight_date)
    except ValueError:
        return [f"Дата рейса: не разобрал {flight_date!r}"]

    errors: list[str] = []
    counts = {"adult": 0, "child": 0, "infant": 0}

    for pax in passengers:
        explicit = (pax.get("type") or "").strip().lower()
        if explicit in counts:
            counts[explicit] += 1
            continue
        try:
            counts[pax_type_on_date(pax["dob"], on_date)] += 1
        except (ValueError, KeyError):
            name = f"{pax.get('first_name', '')} {pax.get('last_name', '')}".strip()
            errors.append(f"{name or 'пассажир'}: не разобрал дату рождения")

    # Требование перевозчиков: ребёнок не летит и не едет один.
    if (counts["child"] or counts["infant"]) and not counts["adult"]:
        errors.append("Для ребёнка или младенца нужен хотя бы один взрослый в списке")

    # Младенец летит на руках, поэтому взрослых должно быть не меньше.
    if counts["infant"] > counts["adult"]:
        errors.append(
            f"Младенцев ({counts['infant']}) больше, чем взрослых ({counts['adult']}) — "
            f"каждый младенец должен быть с отдельным взрослым"
        )

    return errors


def export_for_bot(passengers: list[dict], target_dir: Path) -> tuple[Path, str]:
    """
    Записать passengers.json для бота. Возвращает (путь, значение PASSENGER_IDS).

    Боты читают пассажиров из файла и выбирают нужных по PASSENGER_IDS. Веб
    отдаёт им ровно тех, кто выбран в процессе, поэтому в файл пишем только их,
    а порядок сохраняем — он влияет на распределение мест.
    """
    import json

    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "passengers.json"
    path.write_text(
        json.dumps(passengers, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path, ",".join(str(pax["id"]) for pax in passengers)
