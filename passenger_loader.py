"""Загрузка пассажиров из passengers.json по PASSENGER_IDS."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent

PAX_TYPE_ALIASES = {
    "adult": "adult",
    "взрослый": "adult",
    "child": "child",
    "chd": "child",
    "teen": "child",
    "подросток": "child",
    "infant": "infant",
    "inf": "infant",
    "baby": "infant",
    "ребенок": "infant",
    "ребёнок": "infant",
}

# поля пассажира → ключи в JSON
PASSENGER_FIELDS = (
    "first_name",
    "last_name",
    "dob",
    "gender",
    "doc_number",
    "doc_expdt",
    "email",
    "phone",
    "citizenship",
)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def passengers_file_path() -> Path:
    raw = _env("PASSENGERS_FILE") or "passengers.json"
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def load_passengers_list(path: Path | None = None) -> list[dict]:
    path = path or passengers_file_path()
    if not path.exists():
        raise FileNotFoundError(f"Нет файла пассажиров: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "passengers" in raw:
        items = raw["passengers"]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("passengers.json: нужен список или {\"passengers\": [...]}")
    if not isinstance(items, list):
        raise ValueError("passengers: ожидается список")
    return items


def normalize_pax_type(raw: str | None) -> str:
    key = (raw or "").strip().lower()
    if not key:
        raise ValueError("empty type")
    pax_type = PAX_TYPE_ALIASES.get(key)
    if not pax_type:
        allowed = ", ".join(sorted({v for v in PAX_TYPE_ALIASES.values()}))
        raise RuntimeError(f"Неизвестный type={raw!r} — нужен один из: {allowed}")
    return pax_type


def parse_dob(raw: str) -> dt.date:
    text = (raw or "").strip()
    if not text:
        raise ValueError("пустой dob")
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    m = re.match(r"^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})$", text)
    if m:
        day, month, year = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return dt.date(year, month, day)
    raise ValueError(f"Не разобрал dob: {raw!r}")


def flight_reference_date() -> dt.date:
    """Дата рейса для расчёта возраста — первая из DATES или сегодня."""
    dates_raw = _env("DATES")
    if dates_raw:
        first = dates_raw.split(",")[0].strip()
        try:
            return parse_dob(first)
        except ValueError:
            pass
    return dt.date.today()


def age_on_date(dob: dt.date, on: dt.date) -> int:
    years = on.year - dob.year
    if (on.month, on.day) < (dob.month, dob.day):
        years -= 1
    return years


def infer_pax_type_from_dob(dob_str: str, *, on_date: dt.date | None = None) -> str:
    """
    Внутренние рейсы T5 (ASB-MYP и т.д.):
    infant — 0–4 года, child — 5–17, adult — 18+.
    """
    on = on_date or flight_reference_date()
    age = age_on_date(parse_dob(dob_str), on)
    if age < 5:
        return "infant"
    if age < 18:
        return "child"
    return "adult"


def resolve_pax_type(found: dict, dob: str) -> tuple[str, str]:
    """
    type из JSON, иначе auto по dob.
    Возвращает (type, source) где source = 'json' | 'dob'.
    """
    raw_type = (found.get("type") or "").strip()
    if raw_type:
        return normalize_pax_type(raw_type), "json"
    return infer_pax_type_from_dob(dob), "dob"


def parse_passenger_ids(raw: str | None = None) -> list[str]:
    """
    PASSENGER_IDS=1 — один пассажир.
    PASSENGER_IDS=1,2 — два на один рейс.
    """
    if raw is None:
        raw = _env("PASSENGER_IDS") or "1"
    ids = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    if not ids:
        raise RuntimeError("PASSENGER_IDS пуст — укажи id из passengers.json, напр. PASSENGER_IDS=1")
    return ids


def get_passenger_ids() -> list[str]:
    return parse_passenger_ids()


def get_passenger_counts_by_type(*, verbose: bool = True) -> tuple[int, int, int]:
    """(adult, child 5-12, infant 0-5) для формы поиска T5."""
    passengers = get_passengers(verbose=verbose)
    adults = sum(1 for p in passengers if p["type"] == "adult")
    child = sum(1 for p in passengers if p["type"] == "child")
    infant = sum(1 for p in passengers if p["type"] == "infant")
    if (child or infant) and adults < 1:
        raise RuntimeError(
            "Для ребёнка/подростка нужен хотя бы один взрослый в PASSENGER_IDS"
        )
    return max(1, adults), child, infant


def get_passengers(*, verbose: bool = True) -> list[dict]:
    """Все пассажиры брони из PASSENGER_IDS."""
    pax_list = [get_passenger_by_id(pid, verbose=verbose) for pid in get_passenger_ids()]
    if verbose and len(pax_list) > 1:
        ids = ", ".join(str(p["id"]) for p in pax_list)
        names = "; ".join(
            f"{p['type']}:{p['first_name']} {p['last_name']}" for p in pax_list
        )
        print(f"[passenger] бронь на {len(pax_list)} пасс.: id={ids} | {names}")
    return pax_list


def format_passenger_names(*, verbose: bool = False) -> str:
    """Имена пассажиров для компактного лога (без id/type)."""
    pax_list = get_passengers(verbose=verbose)
    return ", ".join(f"{p['first_name']} {p['last_name']}".strip() for p in pax_list)


def get_passengers_by_type() -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {"adult": [], "child": [], "infant": []}
    for pax in get_passengers():
        grouped[pax["type"]].append(pax)
    return grouped


def get_passenger_by_id(passenger_id: str | None = None, *, verbose: bool = True) -> dict:
    """Берёт пассажира по id из passengers.json (без id — первый из PASSENGER_IDS)."""
    if passenger_id is not None:
        pid = str(passenger_id).strip()
    else:
        pid = get_passenger_ids()[0]

    items = load_passengers_list()
    found = None
    for item in items:
        if str(item.get("id", "")).strip() == pid:
            found = item
            break
    if found is None:
        ids = [str(x.get("id")) for x in items]
        raise RuntimeError(
            f"PASSENGER_IDS: id={pid!r} не найден в {passengers_file_path().name}. "
            f"Доступны: {', '.join(ids)}"
        )

    dob = str(found.get("dob") or found.get("date_of_birth") or "").strip()
    pax_type, type_src = resolve_pax_type(found, dob)
    data = {
        "id": str(found.get("id", pid)),
        "type": pax_type,
        "first_name": str(found.get("first_name") or found.get("first") or "").strip(),
        "last_name": str(found.get("last_name") or found.get("last") or "").strip(),
        "dob": dob,
        "gender": str(found.get("gender") or "M").strip(),
        "doc_number": str(found.get("doc_number") or found.get("passport") or "").strip(),
        "doc_expdt": str(found.get("doc_expdt") or found.get("doc_expiry") or "").strip(),
        "email": str(found.get("email") or "").strip(),
        "phone": str(found.get("phone") or "").strip(),
        "citizenship": str(found.get("citizenship") or "TM").strip(),
    }
    missing = [k for k in ("first_name", "last_name", "dob", "doc_number") if not data[k]]
    if missing:
        raise RuntimeError(f"У пассажира id={pid} пустые поля: {', '.join(missing)}")
    type_note = "type" if type_src == "json" else f"type←dob ({age_on_date(parse_dob(dob), flight_reference_date())} лет)"
    if verbose:
        print(
            f"[passenger] id={data['id']} ({pax_type}, {type_note}) "
            f"{data['first_name']} {data['last_name']}"
        )
    return data
