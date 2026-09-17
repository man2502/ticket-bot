"""Чтение и запись настроек панели.

Что здесь важного:

* **Секреты не покидают сервер.** Пароль SMTP, ключ SMS и пароли перевозчиков
  лежат в базе зашифрованными и НИКОГДА не отдаются в HTML — форма показывает
  только признак «задано». Пустое поле при сохранении означает «не менять»,
  а не «стереть»: иначе любая правка соседнего поля обнуляла бы пароль.

* **Ошибка расшифровки не молчит.** Если ключ сменился, `get_secret` вернёт
  пустую строку и напечатает предупреждение. Запустить бота с пустым паролем
  хуже, чем не запустить: он честно сходит на сайт, получит отказ авторизации,
  и разбираться придётся уже по логам.
"""

from __future__ import annotations

from typing import Any

from core.slots import CHECK_PERIODS_SEC, normalize_check_period, slot_anchors
from web.crypto import SecretUnreadable, decrypt, encrypt
from web.models import Service, ServiceConfig, Setting

__all__ = [
    "get_setting",
    "get_secret",
    "get_all",
    "save_settings",
    "get_service_config",
    "save_service_config",
    "SECRET_KEYS",
    "MASK",
]

# Что именно шифруется. Список задаётся здесь, а не выводится из флага в базе:
# так забытый флаг не превращается в пароль, лежащий открытым текстом.
SECRET_KEYS = frozenset({"smtp_password", "sms_api_key"})

# То, что видит человек вместо сохранённого секрета.
MASK = "••••••••"


def get_setting(db, key: str, default: str = "") -> str:
    """Обычная настройка. Для секретов есть отдельная функция."""
    setting = db.get(Setting, key)
    if setting is None or setting.value == "":
        return default
    return setting.value


def get_secret(db, key: str) -> str:
    """
    Расшифрованное значение секрета.

    Пустая строка означает «не задано» или «не читается текущим ключом» —
    во втором случае в консоль уходит предупреждение.
    """
    setting = db.get(Setting, key)
    if setting is None or not setting.value:
        return ""
    try:
        return decrypt(setting.value)
    except SecretUnreadable as exc:
        print(f"[settings] {key}: {exc}")
        return ""


def has_secret(db, key: str) -> bool:
    """Задано ли значение. Само значение при этом не расшифровывается."""
    setting = db.get(Setting, key)
    return bool(setting is not None and setting.value)


def get_all(db) -> dict[str, Any]:
    """
    Все настройки для отрисовки формы.

    Секреты заменяются признаком `<ключ>_set` — значения в шаблон не попадают
    вообще, чтобы их нельзя было вытащить из исходного кода страницы.
    """
    values: dict[str, Any] = {}
    for setting in db.query(Setting).all():
        if setting.key in SECRET_KEYS:
            values[f"{setting.key}_set"] = bool(setting.value)
        else:
            values[setting.key] = setting.value
    return values


def save_settings(db, data: dict[str, str]) -> None:
    """
    Сохранить настройки.

    Секрет с пустым значением пропускается: пустое поле в форме означает
    «оставить как было». Стереть секрет можно явным значением `-`.
    """
    for key, raw in data.items():
        value = (raw or "").strip()
        is_secret = key in SECRET_KEYS

        if is_secret:
            if not value or value == MASK:
                continue
            value = "" if value == "-" else encrypt(value)

        setting = db.get(Setting, key)
        if setting is None:
            db.add(Setting(key=key, value=value, is_secret=is_secret))
        else:
            setting.value = value
            setting.is_secret = is_secret

    db.commit()


# --------------------------------------------------------------------------- #
# Настройки сервисов
# --------------------------------------------------------------------------- #

# Поля ServiceConfig с паролями. Правила те же, что и для Setting.
SERVICE_SECRET_FIELDS = ("password_enc", "extra_password_enc")


def get_service_config(db, service: str) -> ServiceConfig:
    """Настройки сервиса; строка создаётся при первом обращении."""
    config = db.get(ServiceConfig, service)
    if config is None:
        config = ServiceConfig(service=service)
        db.add(config)
        db.commit()
    return config


def get_service_secret(db, service: str, field: str) -> str:
    """Расшифрованный пароль сервиса (`password_enc` / `extra_password_enc`)."""
    config = get_service_config(db, service)
    raw = getattr(config, field, "")
    if not raw:
        return ""
    try:
        return decrypt(raw)
    except SecretUnreadable as exc:
        print(f"[settings] {service}.{field}: {exc}")
        return ""


def save_service_config(db, service: str, data: dict[str, Any]) -> ServiceConfig:
    """
    Обновить настройки сервиса.

    Числовые поля приводятся с ограничениями снизу и сверху: интерфейс не должен
    позволять выставить, например, нулевую паузу после HTTP 429 — это гарантия
    бана вместо билета.
    """
    config = get_service_config(db, service)

    for field in ("login", "extra_login"):
        if field in data:
            setattr(config, field, (data[field] or "").strip())

    for field in SERVICE_SECRET_FIELDS:
        if field not in data:
            continue
        value = (data[field] or "").strip()
        if not value or value == MASK:
            # Пустое поле = не менять, иначе правка соседней галочки стирала бы пароль.
            continue
        setattr(config, field, "" if value == "-" else encrypt(value))

    for field in (
        "is_enabled",
        "captcha_manual",
        "auto_book",
    ):
        if field in data:
            setattr(config, field, bool(data[field]))

    if "check_period_sec" in data:
        # Не clamp, а выбор из списка: 45 секунд — ровная сетка, которой у
        # живого человека не бывает, и она видна на стороне сайта.
        config.check_period_sec = normalize_check_period(
            data["check_period_sec"], config.check_period_sec
        )

    limits = {
        # поле: (минимум, максимум)
        "check_at_second": (0, 59),
        # 0 = без ограничения; выше 200 поисков в час сайт начинает отвечать 429.
        "max_searches_per_hour": (0, 200),
        "repeated_error_pause_after": (1, 50),
        # Меньше 30 секунд после 429 — верный способ получить бан вместо билета.
        "rate_limit_pause_sec": (30, 3600),
        "captcha_max_attempts": (1, 20),
        # Больше десяти аккаунтов с одного адреса — верный бан, а не билеты.
        "max_parallel": (1, 10),
    }
    for field, (low, high) in limits.items():
        if field not in data:
            continue
        setattr(config, field, max(low, min(high, _to_int(data[field], getattr(config, field)))))

    db.commit()
    return config


# Подписи периодов. Отдельно от core.slots: там расписание, здесь текст формы.
_PERIOD_LABELS = {
    10: "каждые 10 секунд — 6 раз в минуту",
    30: "каждые 30 секунд — 2 раза в минуту",
    60: "раз в минуту",
    120: "раз в 2 минуты",
}


def check_period_options() -> list[tuple[int, str]]:
    """Варианты для выпадающего списка в форме сервиса."""
    return [(period, _PERIOD_LABELS.get(period, f"каждые {period} с")) for period in CHECK_PERIODS_SEC]


def format_slot_marks(period_sec: int, at_second: int) -> str:
    """Отметки словами: «:00, :30» или «:07 раз в 2 минуты»."""
    marks = ", ".join(f":{sec:02d}" for sec in slot_anchors(period_sec, at_second))
    if normalize_check_period(period_sec) > 60:
        return f"{marks} раз в {normalize_check_period(period_sec) // 60} мин"
    return marks


def _to_int(raw: Any, fallback: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback


def service_config_view(db, service: str) -> dict[str, Any]:
    """Настройки сервиса для шаблона — без паролей, только признак «задан»."""
    config = get_service_config(db, service)
    return {
        "service": service,
        "is_enabled": config.is_enabled,
        "login": config.login,
        "extra_login": config.extra_login,
        "password_set": bool(config.password_enc),
        "extra_password_set": bool(config.extra_password_enc),
        "check_period_sec": config.check_period_sec,
        "period_options": check_period_options(),
        # Готовая подсказка «во сколько именно стреляем»: пересчитывать период
        # в секунды минуты в голове человек не должен.
        "slot_marks": format_slot_marks(config.check_period_sec, config.check_at_second),
        "check_at_second": config.check_at_second,
        "max_searches_per_hour": config.max_searches_per_hour,
        "repeated_error_pause_after": config.repeated_error_pause_after,
        "rate_limit_pause_sec": config.rate_limit_pause_sec,
        "captcha_manual": config.captcha_manual,
        "captcha_max_attempts": config.captcha_max_attempts,
        "max_parallel": config.max_parallel,
        "auto_book": config.auto_book,
    }


def missing_credentials(db, service: str) -> list[str]:
    """
    Чего не хватает, чтобы сервис мог работать.

    У railway авторизации нет вовсе — API открытый. У airlines нужны телефон,
    пароль личного кабинета и пароль приложения: по ним бот сам добывает
    Bearer-токены и обновляет их, когда те протухают.
    """
    if service != Service.AIRLINES.value:
        return []

    from web.services.providers import list_providers, provider_missing

    config = get_service_config(db, service)
    missing: list[str] = []
    # Формулировки совпадают с подписями в форме — иначе человек ищет поле,
    # которого на странице нет.
    if not config.extra_password_enc:
        missing.append("пароль учётки приложения")
    # Аккаунты теперь у провайдеров: хватит одного включённого и заполненного.
    ready = [
        p for p in list_providers(db, service, enabled_only=True) if not provider_missing(p)
    ]
    if not ready:
        missing.append("хотя бы один аккаунт T5 с телефоном и паролем")
    return missing
