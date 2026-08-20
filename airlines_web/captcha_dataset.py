"""Сбор датасета капчи T5: confirmed (принятые) и rejected (неверный ввод)."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATASET_DIR = ROOT / "captcha_dataset"
CONFIRMED_DIR = DATASET_DIR / "confirmed"
REJECTED_DIR = DATASET_DIR / "rejected"
UNCERTAIN_DIR = DATASET_DIR / "uncertain"
HARD_DIR = DATASET_DIR / "hard"


def _enabled(name: str, default: str = "1") -> bool:
    return (os.getenv(name) or default).strip().lower() not in ("0", "false", "no")


def collect_enabled() -> bool:
    """CAPTCHA_COLLECT=1 — сохранять принятые сайтом капчи в confirmed/."""
    return _enabled("CAPTCHA_COLLECT", "1")


def collect_rejected_enabled() -> bool:
    """CAPTCHA_COLLECT_REJECTED=1 — сохранять отклонённые (неверный код) в rejected/."""
    if not collect_enabled():
        return False
    return _enabled("CAPTCHA_COLLECT_REJECTED", "1")


def _norm_code(code: str) -> str:
    return re.sub(r"\D", "", (code or "").strip())


def _img_hash(image_bytes: bytes) -> str:
    return hashlib.sha1(image_bytes).hexdigest()[:12]


def _hash_from_filename(path: Path) -> str | None:
    m = re.match(r"^\d{6}_([a-f0-9]{12})\.png$", path.name, re.I)
    return m.group(1).lower() if m else None


def _code_from_filename(path: Path) -> str | None:
    m = re.match(r"^(\d{6})_", path.name)
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
    """Easy-капчи: captcha_dataset/confirmed/ (принятые сайтом)."""
    return _list_png_samples(CONFIRMED_DIR)


def list_hard_samples() -> list[tuple[Path, str]]:
    """Hard-капчи: captcha_dataset/hard/ (overlap, из label_rejected.py)."""
    return _list_png_samples(HARD_DIR)


def list_training_samples() -> list[tuple[Path, str, bool]]:
    """
    Все примеры для обучения: confirmed/ (easy) + hard/ (overlap).
    Без дублей по hash — если файл есть в hard/, из confirmed/ не берётся.
    """
    hard = list_hard_samples()
    hard_hashes = {_hash_from_filename(p) for p, _ in hard}
    hard_hashes.discard(None)

    rows: list[tuple[Path, str, bool]] = [(p, c, True) for p, c in hard]
    for path, code in list_confirmed_samples():
        h = _hash_from_filename(path)
        if h and h in hard_hashes:
            continue
        rows.append((path, code, False))
    return rows


def confirmed_count() -> int:
    return len(list_confirmed_samples())


def hard_count() -> int:
    return len(list_hard_samples())


def mark_hard(
    *,
    image_bytes: bytes | None = None,
    code: str | None = None,
    source_path: Path | None = None,
) -> Path | None:
    """Сохранить капчу в captcha_dataset/hard/."""
    data: bytes | None = None
    digits = _norm_code(code or "")

    if source_path and source_path.is_file():
        data = source_path.read_bytes()
        if len(digits) != 6:
            digits = _code_from_filename(source_path) or digits
    elif image_bytes:
        data = image_bytes

    if not data or len(digits) != 6:
        return None

    h = _img_hash(data)
    HARD_DIR.mkdir(parents=True, exist_ok=True)
    dest = HARD_DIR / f"{digits}_{h}.png"
    if dest.exists():
        return dest
    dest.write_bytes(data)
    return dest


def save_confirmed(image_bytes: bytes, code: str, *, source: str = "cnn") -> Path | None:
    """
    Подтверждённый сайтом пример — датасет для обучения CNN.
    Вызывать только когда капча реально принята (/flights открылся).
    """
    if not collect_enabled() or not image_bytes:
        return None
    digits = _norm_code(code)
    if len(digits) != 6:
        return None
    CONFIRMED_DIR.mkdir(parents=True, exist_ok=True)
    h = _img_hash(image_bytes)
    path = CONFIRMED_DIR / f"{digits}_{h}.png"
    if path.exists():
        return path
    path.write_bytes(image_bytes)
    n = len(list_confirmed_samples())
    print(f"[dataset] +1 confirmed → {path.name} (всего {n})")
    return path


def save_rejected(
    image_bytes: bytes,
    code: str,
    *,
    source: str = "cnn",
    reason: str = "wrong",
) -> Path | None:
    """
    Капча отправлена на сайт, но отклонена (неверный код / ошибка формы).
    Код в имени файла — тот, что ввели (неправильный).
    """
    if not collect_rejected_enabled() or not image_bytes:
        return None
    digits = _norm_code(code)
    if len(digits) != 6:
        return None
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    h = _img_hash(image_bytes)
    safe_reason = re.sub(r"[^\w\-]+", "_", (reason or "wrong").strip())[:24] or "wrong"
    safe_source = re.sub(r"[^\w\-]+", "_", (source or "solver").strip())[:16] or "solver"
    path = REJECTED_DIR / f"{digits}_{safe_source}_{safe_reason}_{h}.png"
    if path.exists():
        return path
    path.write_bytes(image_bytes)
    n = len(list_rejected_samples())
    print(f"[dataset] +1 rejected ({safe_source}, {safe_reason}) → {path.name} (всего {n})")
    return path


def parse_rejected_filename(path: Path) -> dict[str, str] | None:
    """Разбор имени rejected: wrong_code, source, reason, hash."""
    m = re.match(r"^(\d{6})_([^_]+)_([^_]+)_([a-f0-9]{12})\.png$", path.name, re.I)
    if not m:
        return None
    return {
        "wrong_code": m.group(1),
        "source": m.group(2),
        "reason": m.group(3),
        "hash": m.group(4),
    }


def list_rejected_samples() -> list[Path]:
    """Все PNG в rejected/, от старых к новым."""
    if not REJECTED_DIR.exists():
        return []
    return sorted(REJECTED_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)


def promote_rejected_to_confirmed(
    rejected_path: Path,
    code: str,
    *,
    remove_source: bool = True,
) -> Path | None:
    """
    Ручная разметка: правильный код → captcha_dataset/hard/ (overlap).
    rejected удаляется. confirmed/ не трогаем — easy и hard в разных папках.
    """
    if not rejected_path.is_file():
        return None
    digits = _norm_code(code)
    if len(digits) != 6:
        return None
    data = rejected_path.read_bytes()
    dest = mark_hard(image_bytes=data, code=digits)
    if not dest:
        return None
    if remove_source and rejected_path.exists():
        rejected_path.unlink()
    print(
        f"[dataset] размечено → hard/{dest.name} "
        f"(confirmed: {confirmed_count()}, hard: {hard_count()})"
    )
    return dest


def move_rejected_to_uncertain(rejected_path: Path) -> Path | None:
    """
    Сомнительная капча — не в confirmed и не в очереди rejected.
    Без метки, только для архива / возможной разметки позже.
    """
    if not rejected_path.is_file():
        return None
    UNCERTAIN_DIR.mkdir(parents=True, exist_ok=True)
    dest = UNCERTAIN_DIR / rejected_path.name
    if dest.exists():
        rejected_path.unlink(missing_ok=True)
        print(f"[dataset] uncertain уже есть → {dest.name}")
        return dest
    rejected_path.rename(dest)
    n = len(list(UNCERTAIN_DIR.glob("*.png")))
    print(f"[dataset] → uncertain/{dest.name} (всего {n}, не для обучения)")
    return dest



def training_count() -> int:
    return len(list_training_samples())


def auto_train_threshold() -> int:
    try:
        return int((os.getenv("CAPTCHA_AUTO_TRAIN_MIN") or "50").strip() or "50")
    except ValueError:
        return 50


def auto_train_enabled() -> bool:
    return _enabled("CAPTCHA_AUTO_TRAIN", "0")


_last_auto_train_at = 0


def maybe_auto_train() -> None:
    """Переобучить CNN, если накопилось достаточно новых подтверждённых примеров."""
    global _last_auto_train_at
    if not auto_train_enabled():
        return
    n = training_count()
    thr = auto_train_threshold()
    if n < thr:
        return
    if n - _last_auto_train_at < thr:
        return
    _last_auto_train_at = n
    try:
        from train_captcha_cnn import train

        print(f"[dataset] авто-обучение CNN ({n} примеров: confirmed + hard)…")
        train(min_samples=min(20, thr))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] авто-обучение CNN: {exc}")
