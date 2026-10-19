"""Датасет API-капчи T5 (5 цифр, iOS)."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# В контейнере набор живёт в томе с данными (CAPTCHA_API_DATASET_DIR), иначе
# пересборка образа стёрла бы десятки тысяч размеченных картинок.
DATASET_DIR = Path(os.getenv("CAPTCHA_API_DATASET_DIR") or ROOT / "captcha_dataset_api")
CONFIRMED_DIR = DATASET_DIR / "confirmed"
HARD_DIR = DATASET_DIR / "hard"
REJECTED_DIR = DATASET_DIR / "rejected"
UNLABELED_DIR = DATASET_DIR / "unlabeled"
N_DIGITS = 5


def _enabled(name: str, default: str = "1") -> bool:
    return (os.getenv(name) or default).strip().lower() not in ("0", "false", "no")


def collect_enabled() -> bool:
    return _enabled("CAPTCHA_API_COLLECT", "1")


def collect_rejected_enabled() -> bool:
    if not collect_enabled():
        return False
    return _enabled("CAPTCHA_API_COLLECT_REJECTED", "1")


def _norm_code(code: str) -> str:
    return re.sub(r"\D", "", (code or "").strip())


def _img_hash(image_bytes: bytes) -> str:
    return hashlib.sha1(image_bytes).hexdigest()[:12]


def _code_from_filename(path: Path) -> str | None:
    m = re.match(rf"^(\d{{{N_DIGITS}}})_", path.name)
    return m.group(1) if m else None


def _hash_from_filename(path: Path) -> str | None:
    m = re.search(r"_([a-f0-9]{12})\.png$", path.name, re.I)
    return m.group(1) if m else None


def _list_png_samples(folder: Path) -> list[tuple[Path, str]]:
    if not folder.exists():
        return []
    out: list[tuple[Path, str]] = []
    for path in sorted(folder.glob("*.png")):
        code = _code_from_filename(path)
        if code:
            out.append((path, code))
    return out


def list_confirmed_samples() -> list[tuple[Path, str]]:
    return _list_png_samples(CONFIRMED_DIR)


def list_hard_samples() -> list[tuple[Path, str]]:
    return _list_png_samples(HARD_DIR)


def list_training_samples() -> list[tuple[Path, str]]:
    """confirmed/ (easy) + hard/ (overlap), без дублей по hash."""
    hard = list_hard_samples()
    hard_hashes = {_hash_from_filename(p) for p, _ in hard}
    hard_hashes.discard(None)
    rows = [(p, c) for p, c in hard]
    for path, code in list_confirmed_samples():
        h = _hash_from_filename(path)
        if h and h in hard_hashes:
            continue
        rows.append((path, code))
    return rows


def confirmed_count() -> int:
    return len(list_confirmed_samples())


def hard_count() -> int:
    return len(list_hard_samples())


def save_confirmed(image_bytes: bytes, code: str, *, source: str = "cnn-api") -> Path | None:
    if not collect_enabled() or not image_bytes:
        return None
    digits = _norm_code(code)
    if len(digits) != N_DIGITS:
        return None
    CONFIRMED_DIR.mkdir(parents=True, exist_ok=True)
    h = _img_hash(image_bytes)
    path = CONFIRMED_DIR / f"{digits}_{h}.png"
    if path.exists():
        return path
    path.write_bytes(image_bytes)
    print(f"[dataset-api] +1 confirmed → {path.name} (всего {confirmed_count()})")
    return path


def save_rejected(
    image_bytes: bytes,
    code: str,
    *,
    source: str = "cnn-api",
    reason: str = "wrong",
) -> Path | None:
    if not collect_rejected_enabled() or not image_bytes:
        return None
    digits = _norm_code(code)
    if len(digits) != N_DIGITS:
        return None
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    h = _img_hash(image_bytes)
    safe_reason = re.sub(r"[^\w\-]+", "_", (reason or "wrong").strip())[:24] or "wrong"
    path = REJECTED_DIR / f"{digits}_{source}_{safe_reason}_{h}.png"
    if path.exists():
        return path
    path.write_bytes(image_bytes)
    print(f"[dataset-api] +1 rejected → {path.name}")
    return path


def mark_hard(
    *,
    image_bytes: bytes | None = None,
    code: str | None = None,
    source_path: Path | None = None,
) -> Path | None:
    """Сохранить капчу в captcha_dataset_api/hard/."""
    data: bytes | None = None
    digits = _norm_code(code or "")

    if source_path and source_path.is_file():
        data = source_path.read_bytes()
        if len(digits) != N_DIGITS:
            digits = _code_from_filename(source_path) or digits
    elif image_bytes:
        data = image_bytes

    if not data or len(digits) != N_DIGITS:
        return None

    h = _img_hash(data)
    HARD_DIR.mkdir(parents=True, exist_ok=True)
    dest = HARD_DIR / f"{digits}_{h}.png"
    if dest.exists():
        return dest
    dest.write_bytes(data)
    return dest


def parse_rejected_filename(path: Path) -> dict[str, str] | None:
    """
    Разбор имени rejected: wrong_code, source, reason, hash.

    reason берётся жадно, до хеша, потому что сам он может содержать
    подчёркивания: `save_rejected` вызывается с `mismatch_manual`
    и `server_mismatch`. Со строгим `[^_]+` разбирался только `invalid`,
    и для двух причин из трёх мета молча терялась — разметка показывала
    «код неизвестен» там, где вариант модели на самом деле был.
    """
    m = re.match(
        rf"^(\d{{{N_DIGITS}}})_([^_]+)_(.+)_([a-f0-9]{{12}})\.png$",
        path.name,
        re.I,
    )
    if not m:
        return None
    return {
        "wrong_code": m.group(1),
        "source": m.group(2),
        "reason": m.group(3),
        "hash": m.group(4),
    }


def list_rejected_samples() -> list[Path]:
    if not REJECTED_DIR.exists():
        return []
    return sorted(REJECTED_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)


def promote_rejected_to_hard(
    rejected_path: Path,
    code: str,
    *,
    remove_source: bool = True,
) -> Path | None:
    """Ручная разметка: правильный код → hard/, rejected удаляется."""
    if not rejected_path.is_file():
        return None
    digits = _norm_code(code)
    if len(digits) != N_DIGITS:
        return None
    data = rejected_path.read_bytes()
    dest = mark_hard(image_bytes=data, code=digits)
    if not dest:
        return None
    if remove_source and rejected_path.exists():
        rejected_path.unlink()
    print(
        f"[dataset-api] размечено → hard/{dest.name} "
        f"(confirmed: {confirmed_count()}, hard: {hard_count()})"
    )
    return dest


def save_unlabeled(image_bytes: bytes) -> Path | None:
    """Картинка без кода — для ручной разметки label_api_captcha.py."""
    if not collect_enabled() or not image_bytes:
        return None
    UNLABELED_DIR.mkdir(parents=True, exist_ok=True)
    h = _img_hash(image_bytes)
    path = UNLABELED_DIR / f"unlabeled_{h}.png"
    if path.exists():
        return path
    path.write_bytes(image_bytes)
    print(f"[dataset-api] +1 unlabeled → {path.name}")
    return path
