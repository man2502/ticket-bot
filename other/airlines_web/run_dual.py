#!/usr/bin/env python3
"""
Два браузера (acc1 :00 + acc2 :30), один аккаунт из .env.

  ../.venv/bin/python run_dual.py                    # .env — пассажир 1
  ../.venv/bin/python run_dual.py --env profiles/pax2.env   # клон .env — пассажир 2
  ../.venv/bin/python run_dual.py --profile pax2     # только маршрут/пассажир поверх .env

  ../.venv/bin/python run_dual.py --same           # один терминал
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time

from _bootstrap import WEB_ROOT, reexec_with_project_venv, setup_paths

reexec_with_project_venv()
setup_paths()

from core.bootstrap import venv_python  # noqa: E402
from cli_env import parse_book_cli  # noqa: E402
from dual_coord import clear_dual_stop, ensure_dual_stop_scope  # noqa: E402


def _python() -> str:
    """Интерпретатор для дочерних процессов: из .venv, иначе текущий."""
    # Раскладка .venv различается по ОС (bin/python и Scripts/python.exe),
    # поэтому выбор делает core.bootstrap, а не захардкоженный путь.
    return str(venv_python() or sys.executable)


def _book_cmd(py: str, account: str, cli: dict) -> list[str]:
    book = str(WEB_ROOT / "book.py")
    cmd = [py, book, "--account", account]
    if cli.get("profile"):
        cmd.extend(["--profile", cli["profile"]])
    elif cli.get("env_path") is not None:
        cmd.extend(["--env", str(cli["env_path"])])
    return cmd


def _book_shell_prefix(stop_file: str) -> str:
    cd = shlex.quote(str(WEB_ROOT))
    stop_q = shlex.quote(stop_file)
    return (
        f"cd {cd} && "
        f"export PYTHONUNBUFFERED=1 && "
        f"export DUAL_STOP_FILE={stop_q} && "
    )


def _pipe_lines(proc: subprocess.Popen, tag: str, color: str) -> None:
    assert proc.stdout is not None
    reset = "\033[0m"
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            sys.stdout.write(f"{color}[{tag}]{reset} {line}\n")
            sys.stdout.flush()
    except Exception:
        pass


def _applescript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _open_macos_terminal(tag: str, cmd: list[str], py: str, label: str, *, stop_file: str) -> None:
    cmd_q = " ".join(shlex.quote(x) for x in cmd)
    shell_cmd = (
        f"{_book_shell_prefix(stop_file)}"
        f'printf "\\033]0;T5 {tag}\\007" && '
        f"echo '=== T5 {tag} | {label} ===' && "
        f"{cmd_q}; "
        f"ec=$?; "
        f"echo; echo \"[dual] {tag} завершён (код $ec) — окно можно закрыть\"; "
        f'exec "${{SHELL:-/bin/zsh}}"'
    )
    script = (
        'tell application "Terminal"\n'
        "    activate\n"
        f'    do script "{_applescript_escape(shell_cmd)}"\n'
        "end tell"
    )
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "osascript failed")


def _run_separate_windows(cli: dict, label: str, *, stop_file: str) -> int:
    py = _python()
    print("=== T5 DUAL: два окна Terminal + два Chrome ===")
    print(f"[dual] {label}")
    print(f"[dual] stop-файл: {stop_file}")
    print("[dual] acc1 (:00) + acc2 (:30)\n")
    try:
        _open_macos_terminal(
            "acc1", _book_cmd(py, "acc1", cli), py, label, stop_file=stop_file
        )
        _open_macos_terminal(
            "acc2", _book_cmd(py, "acc2", cli), py, label, stop_file=stop_file
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[dual] не удалось открыть Terminal: {exc}", file=sys.stderr)
        print("[dual] fallback → один терминал (--same)", file=sys.stderr)
        return _run_same_terminal(cli, label, stop_file=stop_file)
    print("[dual] готово — смотри два новых окна Terminal.")
    print("[dual] этот процесс можно закрыть; стоп: Ctrl+C в каждом окне.")
    return 0


def _run_same_terminal(cli: dict, label: str, *, stop_file: str) -> int:
    py = _python()
    env_base = os.environ.copy()
    env_base["PYTHONUNBUFFERED"] = "1"
    env_base["DUAL_STOP_FILE"] = stop_file

    jobs = [
        ("acc1", _book_cmd(py, "acc1", cli), "\033[36m"),
        ("acc2", _book_cmd(py, "acc2", cli), "\033[35m"),
    ]

    procs: list[subprocess.Popen] = []
    proc_tags: dict[int, str] = {}
    threads: list[threading.Thread] = []

    print("=== T5 DUAL: один терминал, логи с префиксами ===")
    print(f"[dual] {label}")
    print(f"[dual] stop-файл: {stop_file}")
    print("[dual] Ctrl+C останавливает оба процесса\n")

    for tag, cmd, color in jobs:
        proc = subprocess.Popen(
            cmd,
            cwd=str(WEB_ROOT),
            env=env_base,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        procs.append(proc)
        proc_tags[proc.pid] = tag
        t = threading.Thread(target=_pipe_lines, args=(proc, tag, color), daemon=True)
        t.start()
        threads.append(t)
        print(f"[dual] старт {tag} pid={proc.pid}")

    stopping = False

    def _stop(*_args) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print("\n[dual] стоп — закрываю оба…")
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    done_pids: set[int] = set()
    codes: dict[str, int] = {}
    while len(done_pids) < len(procs) and not stopping:
        for p in procs:
            if p.pid in done_pids:
                continue
            code = p.poll()
            if code is None:
                continue
            done_pids.add(p.pid)
            tag = proc_tags.get(p.pid, "?")
            codes[tag] = code
            print(f"[dual] {tag} завершился (код {code})")
            if code == 0 and not stopping:
                print("[dual] успех — закрываю второй аккаунт…")
                for other in procs:
                    if other.poll() is None:
                        other.send_signal(signal.SIGINT)
        if not stopping:
            time.sleep(0.3)

    for p in procs:
        if p.poll() is None:
            p.wait(timeout=5)

    for t in threads:
        t.join(timeout=2)

    if stopping:
        return 130
    exit_codes = list(codes.values())
    if any(c == 0 for c in exit_codes):
        return 0
    return max(exit_codes) if exit_codes else 1


def main() -> int:
    argv = sys.argv[1:]
    same = "--same" in argv or os.getenv("DUAL_SAME_TERMINAL", "").strip() in (
        "1",
        "true",
        "yes",
    )
    argv = [a for a in argv if a != "--same"]

    cli = parse_book_cli(argv)
    profile = cli.get("profile")
    env_path = cli.get("env_path")

    if env_path is None and profile is None and not (WEB_ROOT / ".env").exists():
        raise SystemExit("Нет .env — создай из .env.example")

    stop_path = ensure_dual_stop_scope(profile=profile, env_path=env_path)
    stop_file = stop_path.name
    clear_dual_stop()

    if profile:
        label = f"--profile {profile}"
    elif env_path is not None:
        label = f"--env {env_path}"
    else:
        label = ".env"

    print(f"[dual] stop-файл: {stop_file}")

    if same or sys.platform != "darwin":
        if not same and sys.platform != "darwin":
            print("[dual] не macOS — логи в одном терминале")
        return _run_same_terminal(cli, label, stop_file=stop_file)
    return _run_separate_windows(cli, label, stop_file=stop_file)


if __name__ == "__main__":
    raise SystemExit(main())
