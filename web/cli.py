"""Команды обслуживания панели.

    python -m web.cli init-db                 создать таблицы и значения по умолчанию
    python -m web.cli create-admin            завести администратора (пароль спросит)
    python -m web.cli create-admin --login a --password ...
    python -m web.cli reset-password --login a
    python -m web.cli list-users
    python -m web.cli export-data --out ticket-data.tar.gz   данные для переезда (см. DEPLOY.md)
    python -m web.cli relocate-paths --old-root D:/projects/ticket

Пароль по умолчанию не заводится намеренно: панель управляет реальными деньгами
(бронь ведёт на страницу оплаты), и учётная запись admin/admin, которую забыли
сменить, — самый частый способ это потерять.
"""

from __future__ import annotations

import argparse
import getpass
import sys

# web.db тянет web.config, а тот кладёт корень репозитория в sys.path —
# отдельный импорт ради путей не нужен.
from web.db import init_db, session_scope
from web.models import Role, User
from web.security import hash_password

MIN_PASSWORD_LEN = 8


def _ask_password(prompt: str = "Пароль: ") -> str:
    """Спросить пароль дважды, не показывая ввод."""
    while True:
        first = getpass.getpass(prompt)
        if len(first) < MIN_PASSWORD_LEN:
            print(f"Слишком короткий пароль: нужно минимум {MIN_PASSWORD_LEN} символов")
            continue
        second = getpass.getpass("Ещё раз: ")
        if first != second:
            print("Пароли не совпали")
            continue
        return first


def cmd_init_db(_args: argparse.Namespace) -> int:
    init_db()
    print("База готова.")
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    init_db()

    login = (args.login or "").strip()
    if not login:
        login = input("Логин администратора: ").strip()
    if not login:
        print("Пустой логин", file=sys.stderr)
        return 1

    password = args.password or _ask_password()
    if len(password) < MIN_PASSWORD_LEN:
        print(f"Пароль короче {MIN_PASSWORD_LEN} символов", file=sys.stderr)
        return 1

    with session_scope() as session:
        if session.query(User).filter(User.login == login).one_or_none() is not None:
            print(f"Пользователь {login!r} уже существует", file=sys.stderr)
            return 1
        session.add(
            User(login=login, password_hash=hash_password(password), role=Role.ADMIN.value)
        )

    print(f"Администратор {login!r} создан.")
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    login = (args.login or "").strip()
    if not login:
        print("Укажи --login", file=sys.stderr)
        return 1

    password = args.password or _ask_password(f"Новый пароль для {login}: ")
    if len(password) < MIN_PASSWORD_LEN:
        print(f"Пароль короче {MIN_PASSWORD_LEN} символов", file=sys.stderr)
        return 1

    with session_scope() as session:
        user = session.query(User).filter(User.login == login).one_or_none()
        if user is None:
            print(f"Нет пользователя {login!r}", file=sys.stderr)
            return 1
        user.password_hash = hash_password(password)
        # Заблокированному сбрасывать пароль имеет смысл только вместе с разблокировкой.
        user.is_active = True

    print(f"Пароль для {login!r} обновлён.")
    return 0


def cmd_list_users(_args: argparse.Namespace) -> int:
    with session_scope() as session:
        users = session.query(User).order_by(User.login).all()
        if not users:
            print("Пользователей нет. Заведи админа: python -m web.cli create-admin")
            return 0
        print(f"{'логин':<20} {'роль':<8} {'статус':<12} последний вход")
        for user in users:
            last = user.last_login_at.strftime("%d.%m.%Y %H:%M") if user.last_login_at else "—"
            state = "активен" if user.is_active else "заблокирован"
            print(f"{user.login:<20} {user.role:<8} {state:<12} {last}")
    return 0


def cmd_export_data(args: argparse.Namespace) -> int:
    from pathlib import Path

    from web.migrate import export_data

    out = Path(args.out).resolve()
    print(f"Собираю {out} — набор капч большой, это займёт пару минут…")
    summary = export_data(out, include_dataset=not args.no_dataset)
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"Готово: {size_mb:.0f} МБ. Файлов: " + ", ".join(f"{k} {v}" for k, v in summary.items()))
    print("В архиве ключи шифрования и паспортные данные — передавайте только по scp/ssh.")
    return 0


def cmd_relocate_paths(args: argparse.Namespace) -> int:
    from web import config
    from web.migrate import RELOCATED_MARK, manifest_root, relocate_paths

    mark = config.DATA_DIR / RELOCATED_MARK
    old_root = (args.old_root or "").strip() or manifest_root(config.DATA_DIR.parent)
    if args.if_needed and (mark.exists() or not old_root):
        return 0
    if not old_root:
        print("Укажи --old-root или положи MANIFEST.json рядом с каталогом данных", file=sys.stderr)
        return 1

    init_db()
    with session_scope() as session:
        changed = relocate_paths(session, old_root)
    mark.write_text(old_root + "\n", encoding="utf-8")
    print(f"Пути из {old_root} переписаны: " + ", ".join(f"{k} {v}" for k, v in changed.items()))
    return 0


def main(argv: list[str] | None = None) -> int:
    # Сообщения на русском: без этого консоль Windows в cp1251 печатает мусор
    # вместо «Администратор создан».
    from core.console import enable_utf8_output

    enable_utf8_output()

    parser = argparse.ArgumentParser(prog="python -m web.cli", description="Обслуживание панели")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="создать таблицы").set_defaults(func=cmd_init_db)

    create = sub.add_parser("create-admin", help="завести администратора")
    create.add_argument("--login")
    create.add_argument("--password", help="лучше не указывать: попадёт в историю команд")
    create.set_defaults(func=cmd_create_admin)

    reset = sub.add_parser("reset-password", help="сменить пароль пользователя")
    reset.add_argument("--login", required=True)
    reset.add_argument("--password")
    reset.set_defaults(func=cmd_reset_password)

    sub.add_parser("list-users", help="список пользователей").set_defaults(func=cmd_list_users)

    export = sub.add_parser("export-data", help="архив данных для переезда на другую машину")
    export.add_argument("--out", default="ticket-data.tar.gz")
    export.add_argument("--no-dataset", action="store_true", help="без набора капч (он большой)")
    export.set_defaults(func=cmd_export_data)

    relocate = sub.add_parser("relocate-paths", help="переписать пути старой машины в базе")
    relocate.add_argument("--old-root", help="корень проекта на старой машине (иначе из MANIFEST.json)")
    relocate.add_argument("--if-needed", action="store_true",
                          help="только если есть MANIFEST.json и это ещё не делалось")
    relocate.set_defaults(func=cmd_relocate_paths)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
