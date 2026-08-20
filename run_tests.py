#!/usr/bin/env python3
"""Запуск тестов проекта.

Зачем свой раннер, а не просто pytest. Бот запускают на машинах, где ставить
пакеты некуда или нельзя (боевой ноут без сети, чужой компьютер). Тесты при
этом трогают самое дорогое место — автобронь, — и должны прогоняться перед
каждым запуском, а не «когда будет интернет».

Поэтому:
  * если pytest установлен — просто передаём управление ему (он умнее);
  * если нет — поднимаем минимальную замену, покрывающую ровно то, чем
    пользуются наши тесты: fixture, monkeypatch, tmp_path, raises, approx,
    parametrize.

Замена НЕ претендует на совместимость с pytest вообще. Она обязана вести себя
одинаково с ним на нашем наборе тестов — не более того.

Запуск:
    python run_tests.py              # все тесты репозитория
    python run_tests.py railway      # только папка railway
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import shutil
import sys
import tempfile
import traceback
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parent

# Папки, где вообще имеет смысл искать test_*.py.
TEST_DIRS = ("railway", "airlines_api", "airlines_web", "chapar", "core")


# --------------------------------------------------------------------------
# Мини-замена pytest
# --------------------------------------------------------------------------


class _ApproxFloat:
    """`pytest.approx` в объёме «сравнить два числа с допуском»."""

    def __init__(self, expected: float, *, abs_: float | None = None, rel: float | None = None):
        self.expected = float(expected)
        self.abs = abs_
        self.rel = rel

    def __eq__(self, other: object) -> bool:
        try:
            value = float(other)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return NotImplemented
        # Допуски как у pytest: rel=1e-6 от ожидаемого, но не меньше abs=1e-12.
        abs_tol = self.abs if self.abs is not None else 1e-12
        rel_tol = self.rel if self.rel is not None else 1e-6
        return abs(value - self.expected) <= max(abs_tol, rel_tol * abs(self.expected))

    def __repr__(self) -> str:
        return f"approx({self.expected})"


class _RaisesContext:
    def __init__(self, expected: type[BaseException] | tuple[type[BaseException], ...]):
        self.expected = expected
        self.value: BaseException | None = None

    def __enter__(self) -> "_RaisesContext":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            names = getattr(self.expected, "__name__", str(self.expected))
            raise AssertionError(f"ожидалось исключение {names}, но его не было")
        if not issubclass(exc_type, self.expected):  # type: ignore[arg-type]
            return False  # не наше исключение — пусть падает дальше
        self.value = exc
        return True


class _MonkeyPatch:
    """Подмена атрибутов и переменных окружения с откатом после теста."""

    def __init__(self) -> None:
        self._attrs: list[tuple[Any, str, Any, bool]] = []
        self._env: list[tuple[str, str | None]] = []
        self._cwd: str | None = None

    def setattr(self, target: Any, name: str, value: Any, raising: bool = True) -> None:
        existed = hasattr(target, name)
        if raising and not existed:
            raise AttributeError(f"{target!r} не имеет атрибута {name!r}")
        self._attrs.append((target, name, getattr(target, name, None), existed))
        setattr(target, name, value)

    def delattr(self, target: Any, name: str, raising: bool = True) -> None:
        existed = hasattr(target, name)
        if not existed:
            if raising:
                raise AttributeError(f"{target!r} не имеет атрибута {name!r}")
            return
        self._attrs.append((target, name, getattr(target, name), True))
        delattr(target, name)

    def setenv(self, name: str, value: str, prepend: str | None = None) -> None:
        self._env.append((name, os.environ.get(name)))
        os.environ[name] = str(value)

    def delenv(self, name: str, raising: bool = True) -> None:
        if name not in os.environ:
            if raising:
                raise KeyError(name)
            return
        self._env.append((name, os.environ[name]))
        del os.environ[name]

    def chdir(self, path: Any) -> None:
        if self._cwd is None:
            self._cwd = os.getcwd()
        os.chdir(str(path))

    def undo(self) -> None:
        # Откат в обратном порядке: последняя подмена снимается первой.
        for target, name, old, existed in reversed(self._attrs):
            if existed:
                setattr(target, name, old)
            else:
                try:
                    delattr(target, name)
                except AttributeError:
                    pass
        self._attrs.clear()
        for name, old in reversed(self._env):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        self._env.clear()
        if self._cwd is not None:
            os.chdir(self._cwd)
            self._cwd = None


class _Mark:
    """`@pytest.mark.parametrize(...)` — единственная используемая метка."""

    @staticmethod
    def parametrize(argnames: str | list[str], argvalues: list[Any]) -> Callable:
        names = (
            [n.strip() for n in argnames.split(",")]
            if isinstance(argnames, str)
            else list(argnames)
        )

        def decorator(func: Callable) -> Callable:
            cases = getattr(func, "_parametrize", [])
            cases.append((names, argvalues))
            func._parametrize = cases  # type: ignore[attr-defined]
            return func

        return decorator

    def __getattr__(self, name: str) -> Callable:
        # Прочие метки (skip, slow, …) в нашем наборе не встречаются — не молчим.
        raise AttributeError(f"pytest.mark.{name} не поддержан мини-раннером")


def _fixture(*args: Any, **kwargs: Any) -> Any:
    """`@pytest.fixture` и `@pytest.fixture(...)` — оба варианта вызова."""

    def wrap(func: Callable) -> Callable:
        func._is_fixture = True  # type: ignore[attr-defined]
        func._autouse = bool(kwargs.get("autouse", False))  # type: ignore[attr-defined]
        return func

    if args and callable(args[0]):
        return wrap(args[0])
    return wrap


def _build_pytest_stub() -> ModuleType:
    module = ModuleType("pytest")
    module.fixture = _fixture  # type: ignore[attr-defined]
    module.mark = _Mark()  # type: ignore[attr-defined]
    module.MonkeyPatch = _MonkeyPatch  # type: ignore[attr-defined]
    module.raises = lambda exc, *a, **k: _RaisesContext(exc)  # type: ignore[attr-defined]
    module.approx = lambda value, abs=None, rel=None: _ApproxFloat(  # type: ignore[attr-defined]
        value, abs_=abs, rel=rel
    )

    def _fail(reason: str = "") -> None:
        raise AssertionError(reason or "pytest.fail")

    def _skip(reason: str = "") -> None:
        raise _Skipped(reason)

    module.fail = _fail  # type: ignore[attr-defined]
    module.skip = _skip  # type: ignore[attr-defined]
    return module


class _Skipped(Exception):
    pass


# --------------------------------------------------------------------------
# Сбор и запуск тестов
# --------------------------------------------------------------------------


@contextmanager
def _resolve_fixture(name: str, module: ModuleType, mp: _MonkeyPatch) -> Iterator[Any]:
    """
    Значение одной фикстуры.

    Встроенных всего две (`tmp_path`, `monkeypatch`), остальные ищем в модуле
    теста. Фикстура-генератор поддержана: код после `yield` выполняется как
    teardown.
    """
    if name == "monkeypatch":
        yield mp
        return

    if name == "tmp_path":
        path = Path(tempfile.mkdtemp(prefix="tickettest-"))
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)
        return

    func = getattr(module, name, None)
    if func is None or not getattr(func, "_is_fixture", False):
        raise RuntimeError(f"нет фикстуры {name!r} в {module.__name__}")

    with _call_with_fixtures(func, module, mp) as value:
        yield value


@contextmanager
def _call_with_fixtures(func: Callable, module: ModuleType, mp: _MonkeyPatch) -> Iterator[Any]:
    """Вызвать функцию, подставив её аргументы-фикстуры (рекурсивно)."""
    params = [
        p
        for p in inspect.signature(func).parameters.values()
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]

    from contextlib import ExitStack

    with ExitStack() as stack:
        kwargs = {
            p.name: stack.enter_context(_resolve_fixture(p.name, module, mp))
            for p in params
        }
        result = func(**kwargs)
        if inspect.isgenerator(result):
            value = next(result)
            try:
                yield value
            finally:
                for _ in result:  # догоняем teardown после yield
                    pass
        else:
            yield result


def _autouse_fixtures(module: ModuleType) -> list[Callable]:
    return [
        obj
        for obj in vars(module).values()
        if callable(obj) and getattr(obj, "_autouse", False)
    ]


def _iter_cases(func: Callable) -> Iterator[tuple[str, dict[str, Any]]]:
    """Развернуть parametrize в отдельные случаи. Без него — один пустой случай."""
    groups = getattr(func, "_parametrize", None)
    if not groups:
        yield "", {}
        return
    names, values = groups[0]
    for row in values:
        row_values = row if isinstance(row, (tuple, list)) else (row,)
        yield f"[{', '.join(repr(v) for v in row_values)}]", dict(zip(names, row_values))


def _run_module(path: Path) -> tuple[int, int, list[str]]:
    """Прогнать один файл тестов. Возвращает (успехов, падений, отчёты о падениях)."""
    # pytest в режиме prepend кладёт папку теста в sys.path — без этого старые
    # файлы (они не правят sys.path сами) не находят railway_trips и соседей.
    parent = str(path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        return 0, 1, [f"{path.name}: не удалось загрузить"]
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        return 0, 1, [f"{path.name}: ошибка импорта\n{traceback.format_exc()}"]

    tests = [
        (name, obj)
        for name, obj in sorted(vars(module).items())
        if name.startswith("test_") and callable(obj)
    ]
    autouse = _autouse_fixtures(module)

    passed = failed = 0
    failures: list[str] = []

    for name, func in tests:
        for suffix, extra in _iter_cases(func):
            mp = _MonkeyPatch()
            label = f"{path.name}::{name}{suffix}"
            try:
                from contextlib import ExitStack

                with ExitStack() as stack:
                    for fixture in autouse:
                        stack.enter_context(_call_with_fixtures(fixture, module, mp))

                    params = [
                        p.name
                        for p in inspect.signature(func).parameters.values()
                        if p.name not in extra
                    ]
                    kwargs = dict(extra)
                    for pname in params:
                        kwargs[pname] = stack.enter_context(
                            _resolve_fixture(pname, module, mp)
                        )
                    func(**kwargs)
                passed += 1
            except _Skipped:
                passed += 1
            except Exception:
                failed += 1
                failures.append(f"{label}\n{traceback.format_exc()}")
            finally:
                mp.undo()

    return passed, failed, failures


def _collect(targets: list[str]) -> list[Path]:
    roots = [REPO_ROOT / t for t in targets] if targets else [
        REPO_ROOT / d for d in TEST_DIRS
    ]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.glob("test_*.py")))
    return files


def main(argv: list[str]) -> int:
    if importlib.util.find_spec("pytest") is not None:
        # Настоящий pytest на месте — он и должен прогонять тесты.
        import pytest  # noqa: PLC0415

        return int(pytest.main(argv or [str(REPO_ROOT / d) for d in TEST_DIRS if (REPO_ROOT / d).is_dir()]))

    print("[tests] pytest не установлен — мини-раннер (pip install pytest для полной версии)")
    sys.modules["pytest"] = _build_pytest_stub()

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    files = _collect([a for a in argv if not a.startswith("-")])
    if not files:
        print("[tests] файлов test_*.py не найдено")
        return 1

    total_passed = total_failed = 0
    all_failures: list[str] = []
    for path in files:
        passed, failed, failures = _run_module(path)
        total_passed += passed
        total_failed += failed
        all_failures.extend(failures)
        mark = "ok" if not failed else "FAIL"
        print(f"  {path.parent.name}/{path.name:<38} {passed:>3} ok, {failed} fail  [{mark}]")

    for failure in all_failures:
        print("\n" + "-" * 70)
        print(failure)

    print(f"\n[tests] пройдено {total_passed}, упало {total_failed}")
    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
