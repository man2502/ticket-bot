"""Загрузка .env.local + .env + профиль поиска (profiles/*.env)."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
LOCAL_ENV = ROOT / ".env.local"
BOOK_ENV = ROOT / ".env"
PROFILES_DIR = ROOT / "profiles"


def resolve_profile_path(name: str) -> Path:
    """aliya → profiles/aliya.env ; trips/x → profiles/trips/x.env"""
    raw = (name or "").strip()
    if not raw:
        raise SystemExit("Пустое имя профиля")
    path = Path(raw)
    if not raw.endswith(".env"):
        path = PROFILES_DIR / f"{raw}.env"
    if not path.is_absolute():
        path = ROOT / path
    return path


def load_project_env(
    *,
    book_env: Path | None = None,
    profile: str | None = None,
    require_book: bool = True,
) -> Path:
    """
    1) .env.local — капча, браузер, ключи (всегда)

    --profile NAME → .env (общий T5_*) + profiles/NAME.env (пассажир/аккаунт поверх)
    --env FILE     → только FILE (полный клон .env для другого пассажира)
    без флагов     → .env.local + .env
    """
    if LOCAL_ENV.exists():
        load_dotenv(LOCAL_ENV, override=True)
        print(f"[env] {LOCAL_ENV.name}")
    else:
        print(
            f"[warn] нет {LOCAL_ENV.name} — скопируй: "
            f"cp .env.local.example .env.local"
        )

    overlay: Path | None = None
    full_replace = False

    if profile:
        overlay = resolve_profile_path(profile)
    elif book_env is not None:
        candidate = book_env if book_env.is_absolute() else ROOT / book_env
        if candidate.resolve() != BOOK_ENV.resolve():
            overlay = candidate
            full_replace = True

    if full_replace and overlay is not None:
        if not overlay.exists():
            raise SystemExit(
                f"Нет env-файла: {overlay}\n"
                f"Создай: cp .env profiles/имя.env"
            )
        load_dotenv(overlay, override=True)
        try:
            label = str(overlay.relative_to(ROOT))
        except ValueError:
            label = str(overlay)
        print(f"[env] клон {label} (полная замена .env)")
        result = overlay
    else:
        if BOOK_ENV.exists():
            load_dotenv(BOOK_ENV, override=True)
            print(f"[env] {BOOK_ENV.name}")
        elif require_book:
            raise SystemExit(
                f"Нет {BOOK_ENV.name} — маршрут и T5_*. См. .env.example"
            )

        if overlay is not None:
            if not overlay.exists():
                raise SystemExit(
                    f"Нет профиля: {overlay}\n"
                    f"Создай: cp profiles/example.env profiles/имя.env"
                )
            load_dotenv(overlay, override=True)
            try:
                label = str(overlay.relative_to(ROOT))
            except ValueError:
                label = str(overlay)
            print(f"[env] профиль {label} (поверх .env)")

        result = overlay or BOOK_ENV

    return result
