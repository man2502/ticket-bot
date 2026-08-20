"""Запуск обычного Google Chrome вместо встроенного Chromium Playwright."""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.env import env, env_flag  # noqa: E402

_PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


class ProfileInUseError(RuntimeError):
    """Профиль Chrome уже занят другим процессом."""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _profile_lock_owner_pid(profile_dir: Path) -> int | None:
    lock = profile_dir / "SingletonLock"
    if not lock.exists():
        return None
    try:
        target = os.readlink(lock)
    except OSError:
        return None
    m = re.search(r"-(\d+)$", str(target))
    return int(m.group(1)) if m else None


def _remove_profile_lock_files(profile_dir: Path) -> list[str]:
    removed: list[str] = []
    for name in _PROFILE_LOCK_NAMES:
        path = profile_dir / name
        if not path.exists() and not path.is_symlink():
            continue
        try:
            path.unlink(missing_ok=True)
            removed.append(name)
        except OSError as exc:
            print(f"[warn] не удалось удалить {profile_dir.name}/{name}: {exc}")
    return removed


def ensure_profile_unlocked(profile_dir: Path, *, force_stale: bool = False) -> None:
    """
    Снимает stale Singleton* после аварийного завершения Chrome.
    Если профиль занят живым процессом — ProfileInUseError.
    """
    lock = profile_dir / "SingletonLock"
    if not lock.exists() and not any((profile_dir / n).exists() for n in _PROFILE_LOCK_NAMES):
        return

    pid = _profile_lock_owner_pid(profile_dir)
    if pid is not None and _pid_alive(pid):
        if not force_stale:
            raise ProfileInUseError(
                f"Chrome уже использует профиль {profile_dir.name} (pid {pid}). "
                f"Останови другой book.py/watch.py с тем же аккаунтом или:\n"
                f"  pkill -f '{profile_dir.name}'"
            )
        print(f"[warn] профиль {profile_dir.name} занят pid {pid}, force_stale — снимаю lock")

    removed = _remove_profile_lock_files(profile_dir)
    if removed:
        print(
            f"[browser] снят stale lock ({profile_dir.name}): "
            f"{', '.join(removed)} — прошлый Chrome не закрылся чисто"
        )


def _launch_persistent_context(playwright, *, profile_dir: Path, **kwargs):
    ensure_profile_unlocked(profile_dir)
    try:
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            **kwargs,
        )
    except Exception as exc:
        msg = str(exc)
        if "SingletonLock" not in msg and "ProcessSingleton" not in msg:
            raise
        print("[browser] Chrome не стартовал из‑за lock — снимаю stale и повторяю…")
        ensure_profile_unlocked(profile_dir, force_stale=True)
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            **kwargs,
        )


# env() общий (core.env). Раньше здесь была локальная копия с другим
# поведением на пустом значении: `NOTIFY_EMAIL=` она возвращала как "",
# а notify_helper — как default. Одна строка .env означала разное.
_flag = env_flag


def save_bandwidth() -> bool:
    """SAVE_BANDWIDTH=1 — меньше трафика: блок картинок/шрифтов, кэш не сносим."""
    return _flag("SAVE_BANDWIDTH", "0")


def headless_mode() -> bool:
    """HEADLESS=1 — Chrome без окна (VPS/фон; ручная капча в браузере недоступна)."""
    return _flag("HEADLESS", "0")


def _viewport_from_env() -> dict[str, int]:
    raw = env("BROWSER_WINDOW_SIZE", "1200,900") or "1200,900"
    parts = raw.replace(" ", "").split(",")
    try:
        w, h = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        w, h = 1200, 900
    return {"width": max(800, w), "height": max(600, h)}


_BANDWIDTH_BLOCK_TYPES = frozenset({"image", "media", "font"})
_BANDWIDTH_BLOCKED = 0


def bandwidth_blocked_count() -> int:
    return _BANDWIDTH_BLOCKED


def _allow_bandwidth_url(url: str, resource_type: str) -> bool:
    if resource_type not in _BANDWIDTH_BLOCK_TYPES:
        return True
    u = (url or "").lower()
    if any(x in u for x in ("captcha", "recaptcha", "refresh-captcha")):
        return True
    return False


def install_bandwidth_saver(context) -> None:
    """Блокирует тяжёлые ресурсы; картинки капчи пропускает."""
    # Счётчик инкрементит вложенный _handler, у него свой global — здесь он лишний.
    if not save_bandwidth():
        return

    def _handler(route) -> None:
        global _BANDWIDTH_BLOCKED
        req = route.request
        if _allow_bandwidth_url(req.url, req.resource_type):
            route.continue_()
            return
        _BANDWIDTH_BLOCKED += 1
        route.abort()

    context.route("**/*", _handler)
    print(
        "[browser] SAVE_BANDWIDTH=1: блок картинок/шрифтов/медиа "
        "(капча пропускается); HTTP-кэш при очистке не трогаем"
    )


def effective_clear_every_min() -> float:
    """Интервал очистки; при SAVE_BANDWIDTH минимум 30 мин."""
    try:
        every_min = float((env("CLEAR_BROWSER_EVERY_MIN") or "9").strip() or "9")
    except ValueError:
        every_min = 9.0
    if save_bandwidth() and 0 < every_min < 30:
        return 30.0
    return every_min


def profile_dir_for() -> Path | None:
    """
    Папка persistent-профиля для текущего аккаунта (или None, если профиль выключен).

    default             → .chrome-profile
    CHROME_PROFILE=acc1 → .chrome-profile-acc1
    """
    use_persist = env("USE_PERSISTENT_PROFILE", "1") not in ("0", "false", "no")
    if not use_persist:
        return None
    profile_key = env("CHROME_PROFILE") or env("ACCOUNT_NAME") or "default"
    safe = re.sub(r"[^\w\-]+", "_", profile_key.strip()) or "default"
    if safe == "default":
        return ROOT / ".chrome-profile"
    return ROOT / f".chrome-profile-{safe}"


def wipe_profile_dir() -> None:
    """
    Полностью удаляет папку профиля Chrome и создаёт пустую заново.
    После этого браузер стартует «с нуля»: без cookies, логина и кэша.
    ВАЖНО: вызывать, когда браузер с этим профилем НЕ запущен.
    """
    d = profile_dir_for()
    if d is None:
        print("[browser] persistent-профиль выключен — удалять нечего")
        return
    if d.exists():
        try:
            shutil.rmtree(d)
            print(f"[browser] профиль полностью удалён: {d.name}")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] не удалось удалить профиль {d.name}: {exc}")
    d.mkdir(exist_ok=True)


def clear_browser_data(page, *, origins: list[str] | None = None) -> None:
    """
    Чистит cookies, localStorage, sessionStorage и browser cache (CDP).
    SAVE_BANDWIDTH=1 — HTTP-кэш не трогаем (меньше повторных загрузок сайта).
    После этого нужна повторная авторизация.
    """
    ctx = page.context
    keep_cache = save_bandwidth()
    if keep_cache:
        print("[browser] чищу cookies / storage (HTTP-кэш оставляю — SAVE_BANDWIDTH)…")
    else:
        print("[browser] чищу cookies / cache / storage…")

    try:
        ctx.clear_cookies()
        print("[browser] cookies очищены")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] clear_cookies: {exc}")

    pages = list(ctx.pages) or [page]
    for p in pages:
        try:
            p.evaluate(
                """() => {
                    try { localStorage.clear(); } catch (e) {}
                    try { sessionStorage.clear(); } catch (e) {}
                }"""
            )
        except Exception:
            pass
    print("[browser] localStorage/sessionStorage очищены")

    if not keep_cache:
        try:
            cdp = ctx.new_cdp_session(page)
            try:
                cdp.send("Network.clearBrowserCache")
                print("[browser] Network cache очищен")
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] clearBrowserCache: {exc}")
            try:
                cdp.send("Network.clearBrowserCookies")
            except Exception:
                pass

            targets = origins or [
                "https://turkmenistanairlines.tm",
                "https://www.turkmenistanairlines.tm",
            ]
            for origin in targets:
                try:
                    cdp.send(
                        "Storage.clearDataForOrigin",
                        {"origin": origin, "storageTypes": "all"},
                    )
                    print(f"[browser] storage очищен для {origin}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] clearDataForOrigin {origin}: {exc}")
            try:
                cdp.detach()
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] CDP session: {exc}")

    print("[browser] очистка завершена")


def clear_due(last_clear_at: float) -> tuple[bool, float]:
    """
    Пора ли делать очистку/рестарт? Возвращает (нужно_сейчас, новый_last_clear_at).

    Раз в CLEAR_BROWSER_EVERY_MIN минут (default 9).

    CLEAR_BROWSER_PHASE_MIN — сдвиг по «часовым» минутам (0..every-1),
    чтобы два аккаунта чистились в разные минуты:
      acc1: PHASE=0 → :00, :09, :18, :27…
      acc2: PHASE=4 → :04, :13, :22, :31…
    """
    import datetime as _dt

    every_min = effective_clear_every_min()
    if every_min <= 0:
        return False, last_clear_at

    try:
        phase = int(float((env("CLEAR_BROWSER_PHASE_MIN") or "0").strip() or "0"))
    except ValueError:
        phase = 0
    every_i = max(1, int(every_min))
    phase = phase % every_i

    now = time.time()
    # первая отсечка — только запомнить старт, не чистить сразу
    if last_clear_at <= 0:
        return False, now

    elapsed = now - last_clear_at
    min_gap = every_min * 60
    dt = _dt.datetime.now()
    on_phase_minute = (dt.minute % every_i) == phase

    if elapsed < min_gap:
        # ещё рано, но если попали в «свою» минуту и прошло ≥90с — можно
        if not on_phase_minute or elapsed < 90:
            return False, last_clear_at
    # иначе: либо своя минута, либо просрочили цикл — пора

    print(
        f"[browser] слот очистки "
        f"(каждые {every_i} мин, phase=:{phase:02d}, сейчас {dt.strftime('%H:%M:%S')})"
    )
    return True, now


def launch_browser(playwright):
    """
    Открывает системный Google Chrome (не Playwright Chromium).

    .env:
      BROWSER_CHANNEL=chrome   # chrome | msedge | chromium
      USE_PERSISTENT_PROFILE=1 # профиль рядом со скриптом (как обычный Chrome)
      HEADLESS=1               # без окна (VPS/фон)
      CHROME_PROFILE=acc1      # папка .chrome-profile-acc1 (--account acc1|acc2)
      BROWSER_WINDOW_POS=0,0   # опционально: позиция окна для 2 мониторов/окон
      BROWSER_WINDOW_SIZE=1280,900
    """
    channel = env("BROWSER_CHANNEL", "chrome") or "chrome"
    use_persist = env("USE_PERSISTENT_PROFILE", "1") not in ("0", "false", "no")
    headless = headless_mode()

    launch_args = [
        "--disable-blink-features=AutomationControlled",
    ]
    if save_bandwidth():
        launch_args.extend(
            [
                "--disable-background-networking",
                "--disable-sync",
                "--disable-default-apps",
                "--disable-component-update",
            ]
        )

    # позиция/размер окна — только если не headless
    pos = env("BROWSER_WINDOW_POS")
    size = env("BROWSER_WINDOW_SIZE")
    if not headless:
        if pos:
            launch_args.append(f"--window-position={pos}")
        if size:
            launch_args.append(f"--window-size={size}")
        if not pos and not size:
            launch_args.append("--start-maximized")
    elif headless:
        print("[browser] HEADLESS=1 — Chrome без окна (ручная капча недоступна)")

    if use_persist:
        profile_dir = profile_dir_for() or (ROOT / ".chrome-profile")
        profile_dir.mkdir(exist_ok=True)
        mode = "headless" if headless else "окно"
        print(f"[browser] Google Chrome ({mode}, profile: {profile_dir.name})")
        try:
            context = _launch_persistent_context(
                playwright,
                profile_dir=profile_dir,
                channel=channel,
                headless=headless,
                locale="ru-RU",
                viewport=_viewport_from_env() if headless else None,
                args=launch_args,
                ignore_default_args=["--enable-automation"],
                chromium_sandbox=True,
            )
        except ProfileInUseError as exc:
            print(f"[browser] {exc}")
            raise SystemExit(1) from exc
        page = context.pages[0] if context.pages else context.new_page()
        install_bandwidth_saver(context)
        return context, page, True  # persistent → context.close()

    print(f"[browser] Google Chrome (channel={channel}, без persistent profile)")
    try:
        browser = playwright.chromium.launch(
            channel=channel,
            headless=headless,
            args=launch_args,
            ignore_default_args=["--enable-automation"],
            chromium_sandbox=True,
        )
    except Exception as exc:
        print(f"[warn] Не удалось открыть channel={channel}: {exc}")
        print("[warn] Пробую встроенный Chromium…")
        browser = playwright.chromium.launch(
            headless=headless,
            args=launch_args,
            chromium_sandbox=True,
        )

    context = browser.new_context(
        locale="ru-RU",
        viewport=_viewport_from_env(),
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    )
    page = context.new_page()
    install_bandwidth_saver(context)
    return browser, page, False  # browser.close()
