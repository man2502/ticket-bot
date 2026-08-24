"""Разметка капч, которые CNN не смогла решить.

Как это устроено. Процесс поиска никогда не ждёт человека: не решилась капча —
берём новую, а картинку складываем в датасет. Админ разбирает накопленное
в свободное время, размеченное уходит в обучающую выборку.

Источник правды — файлы в `airlines_api/captcha_dataset_api/`, а не база.
Так сделано, чтобы не поддерживать синхронизацию двух хранилищ: боты пишут
туда напрямую, ничего не зная про панель, и любые расхождения между базой
и файлами означали бы, что размеченное потерялось.

База ведёт только журнал: кто и когда что разметил.

Папки датасета:

    rejected/   CNN дала код, сервер его отверг — есть ошибочный вариант
    unlabeled/  код неизвестен вообще
    hard/       размеченные вручную, идут в обучение с повышенным весом
    confirmed/  капчи, которые сервер принял

Капча есть только у самолётов: у поездов её нет вовсе.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AIRLINES_DIR = REPO_ROOT / "airlines_api"
for _path in (REPO_ROOT, AIRLINES_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from web.models import CaptchaSample, CaptchaStatus, Service  # noqa: E402

__all__ = [
    "CaptchaItem",
    "list_pending",
    "stats",
    "label_sample",
    "discard_sample",
    "sample_path",
    "N_DIGITS",
    "CaptchaError",
]

# На капче T5 всегда пять цифр.
N_DIGITS = 5


class CaptchaError(RuntimeError):
    """Файл не найден, код неверной длины или разметка не удалась."""


@dataclass
class CaptchaItem:
    """Одна картинка, ждущая разметки."""

    name: str
    kind: str  # rejected | unlabeled
    cnn_guess: str = ""
    reason: str = ""
    source: str = ""
    modified: dt.datetime | None = None

    @property
    def hint(self) -> str:
        """Что показать рядом с картинкой."""
        if not self.cnn_guess:
            return "код неизвестен"
        why = {
            "invalid": "сервер отверг",
            "mismatch_manual": "разошлось с ручным вводом",
            "server_mismatch": "сервер принял другой",
        }.get(self.reason, self.reason or "не подошёл")
        return f"вариант CNN: {self.cnn_guess} — {why}"


def _dataset():
    """Модуль датасета бота. Импорт ленивый: он тянет за собой пути и настройки."""
    import captcha_dataset_api  # noqa: PLC0415

    return captcha_dataset_api


def sample_path(name: str) -> Path:
    """
    Путь к картинке по имени файла.

    Имя приходит из адреса страницы, поэтому проверяем, что оно не уводит
    за пределы датасета: `../../secret.key` в имени — классический способ
    прочитать чужой файл через веб.
    """
    data = _dataset()
    if "/" in name or "\\" in name or name.startswith("."):
        raise CaptchaError("недопустимое имя файла")

    for folder in (data.REJECTED_DIR, data.UNLABELED_DIR):
        candidate = folder / name
        if candidate.is_file() and candidate.parent == folder:
            return candidate

    raise CaptchaError("картинка не найдена — возможно, её уже разметили")


def list_pending(limit: int = 200) -> list[CaptchaItem]:
    """
    Картинки, ждущие разметки: сначала те, где CNN ошиблась.

    Они полезнее для обучения: у них уже есть неверный вариант, и модель
    учится именно на своих ошибках.
    """
    data = _dataset()
    items: list[CaptchaItem] = []

    for path in data.list_rejected_samples():
        meta = data.parse_rejected_filename(path) or {}
        items.append(
            CaptchaItem(
                name=path.name,
                kind="rejected",
                cnn_guess=meta.get("wrong_code", ""),
                reason=meta.get("reason", ""),
                source=meta.get("source", ""),
                modified=_modified(path),
            )
        )

    if data.UNLABELED_DIR.exists():
        for path in sorted(data.UNLABELED_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime):
            items.append(CaptchaItem(name=path.name, kind="unlabeled", modified=_modified(path)))

    return items[:limit]


def _modified(path: Path) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def stats() -> dict[str, int]:
    """Сколько чего накопилось — для шапки раздела."""
    data = _dataset()
    unlabeled = (
        len(list(data.UNLABELED_DIR.glob("*.png"))) if data.UNLABELED_DIR.exists() else 0
    )
    return {
        "rejected": len(data.list_rejected_samples()),
        "unlabeled": unlabeled,
        "hard": data.hard_count(),
        "confirmed": data.confirmed_count(),
    }


def normalize_code(raw: str) -> str:
    """Оставить только цифры: люди вводят с пробелами и дефисами."""
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def label_sample(db, name: str, code: str, *, user_id: int | None = None) -> str:
    """
    Записать правильный код и переложить картинку в обучающую выборку.

    Возвращает текст результата. Ошибки поднимает `CaptchaError` —
    их показывают человеку, а не прячут в лог.
    """
    digits = normalize_code(code)
    if len(digits) != N_DIGITS:
        raise CaptchaError(f"Нужно ровно {N_DIGITS} цифр, получено: {digits or '—'}")

    path = sample_path(name)
    data = _dataset()

    if path.parent == data.REJECTED_DIR:
        # rejected → hard: модель учится именно на своих ошибках.
        dest = data.promote_rejected_to_hard(path, digits)
        target = "hard"
    else:
        dest = data.mark_hard(image_bytes=path.read_bytes(), code=digits)
        target = "hard"
        if dest:
            path.unlink(missing_ok=True)

    if not dest:
        raise CaptchaError("Не удалось сохранить в обучающую выборку")

    db.add(
        CaptchaSample(
            service=Service.AIRLINES.value,
            file_path=str(dest),
            cnn_guess=_guess_from_name(name),
            correct_code=digits,
            status=CaptchaStatus.LABELED.value,
            labeled_by_id=user_id,
            labeled_at=dt.datetime.now(),
        )
    )
    db.commit()

    return f"Код {digits} записан, картинка ушла в {target}/"


def discard_sample(db, name: str, *, user_id: int | None = None) -> str:
    """Удалить нечитаемую картинку — в обучении она только мешает."""
    path = sample_path(name)
    guess = _guess_from_name(name)
    stored = str(path)
    path.unlink(missing_ok=True)

    db.add(
        CaptchaSample(
            service=Service.AIRLINES.value,
            file_path=stored,
            cnn_guess=guess,
            status=CaptchaStatus.DISCARDED.value,
            labeled_by_id=user_id,
            labeled_at=dt.datetime.now(),
        )
    )
    db.commit()
    return "Картинка удалена как нечитаемая"


def _guess_from_name(name: str) -> str:
    """Вариант CNN из имени файла rejected — для журнала разметки."""
    head = name.split("_", 1)[0]
    return head if head.isdigit() else ""


def training_hint() -> str:
    """Подсказка про дообучение — команда, которую надо выполнить руками."""
    return (
        "После разметки модель нужно дообучить: "
        "python airlines_api/train_captcha_cnn_api.py --resume"
    )
