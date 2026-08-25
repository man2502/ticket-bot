#!/usr/bin/env python3
"""Обучение CNN для API-капчи T5 (5 цифр): captcha_dataset_api/confirmed/."""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from captcha_cnn_api import (  # noqa: E402
    N_DIGITS,
    build_model,
    build_model_slice,
    init_from_browser_model,
    browser_model_path,
    label_to_tensor,
    load_matching_state,
    model_arch,
    model_path,
    preprocess_image,
    save_metrics,
    save_model,
)
from captcha_dataset_api import confirmed_count, hard_count, list_training_samples  # noqa: E402


def _lazy_train_deps():
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, Dataset

    return torch, nn, DataLoader, Dataset


def _eval_rows(model, rows, *, device) -> tuple[int, int]:
    full_ok = 0
    with __import__("torch").no_grad():
        for path, code in rows:
            x = preprocess_image(path.read_bytes(), augment=False).to(device)
            logits = model(x)
            pred = "".join(str(int(l.argmax(dim=1).item())) for l in logits)
            full_ok += int(pred == code)
    return full_ok, len(rows)


def _split_train_val(rows: list, val_ratio: float = 0.2) -> tuple[list, list]:
    """Train/val split с поддержкой очень маленького датасета."""
    n = len(rows)
    if n <= 1:
        return rows, rows
    shuffled = rows[:]
    random.shuffle(shuffled)
    if n == 2:
        return [shuffled[0]], [shuffled[1]]
    if n <= 4:
        n_val = 1
    else:
        n_val = max(1, int(n * val_ratio))
    n_val = min(n_val, n - 1)
    val_rows = shuffled[:n_val]
    train_rows = shuffled[n_val:]
    return train_rows, val_rows


def _training_profile(total_n: int, epochs: int) -> dict:
    """Параметры обучения под размер датасета."""
    if total_n < 50:
        return {
            "mode": "small",
            "oversample": max(30, 600 // max(total_n, 1)),
            "train_all": True,
            "epochs": max(epochs, 150),
            "lr": 2e-3,
            "weight_decay": 0.0,
            "label_smoothing": 0.0,
            "aug_mix": 0.35,
            "aug_light": True,
            "patience": 50,
            "early_stop_after": 40,
            "pick_by": "all",
        }
    if total_n < 150:
        return {
            "mode": "medium",
            "oversample": 4,
            "train_all": False,
            "epochs": max(epochs, 120),
            "lr": 1e-3,
            "weight_decay": 1e-4,
            "label_smoothing": 0.02,
            "aug_mix": 0.7,
            "aug_light": True,
            "patience": 30,
            "early_stop_after": 35,
            "pick_by": "val",
        }
    return {
        "mode": "full",
        "oversample": 1,
        "train_all": False,
        "epochs": epochs,
        "lr": 8e-4,
        "weight_decay": 1e-3,
        "label_smoothing": 0.05,
        "aug_mix": 1.0,
        "aug_light": False,
        "patience": 20,
        "early_stop_after": 30,
        "pick_by": "val",
    }


def _expand_train_rows(
    rows: list[tuple[Path, str]],
    *,
    oversample: int,
    aug_mix: float,
) -> list[tuple[Path, str, bool]]:
    expanded: list[tuple[Path, str, bool]] = []
    for _ in range(max(1, oversample)):
        for path, code in rows:
            expanded.append((path, code, random.random() < aug_mix))
    random.shuffle(expanded)
    return expanded


def _build_for_arch(arch: str):
    return build_model() if arch == "attn" else build_model_slice()


def _make_optimizer(torch, model, *, lr: float, weight_decay: float, pretrain: bool):
    if not pretrain:
        return torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    backbone = [p for name, p in model.named_parameters() if name.startswith("features.")]
    head = [p for name, p in model.named_parameters() if not name.startswith("features.")]
    return torch.optim.AdamW(
        [
            {"params": backbone, "lr": lr * 0.15},
            {"params": head, "lr": lr},
        ],
        weight_decay=weight_decay,
    )


def train(
    *,
    epochs: int = 100,
    batch_size: int = 8,
    lr: float | None = None,
    val_ratio: float = 0.2,
    min_samples: int = 2,
    out_path: Path | None = None,
    patience: int | None = None,
    resume: bool = False,
    init_from: Path | None = None,
    arch: str = "slice",
    pretrain: bool = True,
) -> int:
    torch, nn, DataLoader, Dataset = _lazy_train_deps()

    all_rows = list_training_samples()
    total_n = len(all_rows)
    if total_n < min_samples:
        print(
            f"[train-api] мало данных: {total_n} < {min_samples}.\n"
            f"  1) Запусти api_check.py — копит unlabeled/\n"
            f"  2) .venv/bin/python airlines_api/label_api_captcha.py\n"
            f"  3) .venv/bin/python airlines_api/label_api_rejected.py\n"
            f"  4) Повтори train"
        )
        return 1

    profile = _training_profile(total_n, epochs)
    effective_epochs = profile["epochs"]
    train_lr = lr if lr is not None else profile["lr"]
    train_patience = patience if patience is not None else profile["patience"]

    if profile["mode"] == "small":
        print(
            f"[train-api] режим small ({total_n} капч): "
            f"oversample×{profile['oversample']}, все в train, лёгкая aug"
        )
    elif total_n < 15:
        print(
            f"[train-api] ⚠ мало confirmed ({total_n}) — точность на новых капчах "
            "может быть низкой"
        )

    random.seed(42)
    if profile["train_all"]:
        base_train = all_rows
        val_rows = all_rows
    else:
        base_train, val_rows = _split_train_val(all_rows, val_ratio=val_ratio)

    train_rows = _expand_train_rows(
        base_train,
        oversample=profile["oversample"],
        aug_mix=profile["aug_mix"],
    )
    print(
        f"[train-api] confirmed={confirmed_count()} hard={hard_count()} "
        f"train={len(base_train)} (×{profile['oversample']}={len(train_rows)}) "
        f"val={len(val_rows)}"
    )

    class _DS(Dataset):
        def __init__(self, rows: list[tuple[Path, str, bool]], *, aug_light: bool):
            self.rows = rows
            self.aug_light = aug_light

        def __len__(self):
            return len(self.rows)

        def __getitem__(self, i):
            path, code, do_aug = self.rows[i]
            x = preprocess_image(
                path.read_bytes(),
                augment=do_aug,
                augment_light=self.aug_light,
            ).squeeze(0)
            y = label_to_tensor(code)
            return x, y

    device = torch.device("cpu")
    train_loader = DataLoader(
        _DS(train_rows, aug_light=profile["aug_light"]),
        batch_size=min(batch_size, max(1, len(train_rows))),
        shuffle=True,
    )
    val_loader = DataLoader(
        _DS([(p, c, False) for p, c in val_rows], aug_light=False),
        batch_size=min(batch_size, max(1, len(val_rows))),
        shuffle=False,
    )

    out = out_path or model_path()

    # Откуда брать стартовые веса. По умолчанию — тот же файл, куда пишем:
    # так `--resume` работал всегда, и для запуска из командной строки это верно.
    #
    # Но при `--out` в отдельный файл это ловушка: первый же прогон не найдёт
    # там ничего и молча начнёт с нуля, выбросив всё, что модель уже умела.
    # Панель именно так и запускает обучение, поэтому источник задаётся явно.
    source = init_from or out

    use_pretrain = pretrain and not resume
    if resume and source.exists():
        try:
            state = torch.load(source, map_location="cpu", weights_only=True)
        except TypeError:
            state = torch.load(source, map_location="cpu")
        ckpt_arch = model_arch(state)
        if arch != ckpt_arch:
            model = _build_for_arch(arch)
            load_matching_state(model, state)
            print(f"[train-api] миграция {ckpt_arch}→{arch} из {source.name}")
        else:
            model = _build_for_arch(arch)
            load_matching_state(model, state)
            print(f"[train-api] fine-tune {arch} из {source.name}")
    else:
        if resume:
            # Это не мелочь: человек просил дообучить, а получит другую модель.
            print(f"[train-api] ВНИМАНИЕ: {source.name} не найден — обучение с нуля")
        model = _build_for_arch(arch)
        if use_pretrain and browser_model_path().exists():
            init_from_browser_model(model)
            print(f"[train-api] {arch} + pretrain из браузерной 6-цифровой CNN")
        elif use_pretrain:
            print(f"[train-api] pretrain: {browser_model_path().name} не найден, с нуля")
        else:
            print(f"[train-api] обучение {arch} с нуля")

    model.to(device)
    opt = _make_optimizer(
        torch,
        model,
        lr=train_lr,
        weight_decay=profile["weight_decay"],
        pretrain=use_pretrain and browser_model_path().exists(),
    )
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(
        opt,
        T_max=effective_epochs,
        eta_min=train_lr * 0.05,
    )
    criterion = nn.CrossEntropyLoss(label_smoothing=profile["label_smoothing"])

    best_score = -1.0
    best_state = None
    stale = 0
    early_stop_after = profile["early_stop_after"]
    # Инициализация нужна для метрик: при --epochs 0 цикл не выполнится ни разу,
    # и обращение к epoch после него упало бы на NameError.
    epoch = 0

    for epoch in range(1, effective_epochs + 1):
        model.train()
        train_loss = train_ok = train_n = 0
        for xb, yb in train_loader:
            xb = xb.to(device)
            yb = yb.to(device)
            opt.zero_grad()
            logits = model(xb)
            loss = sum(criterion(logits[i], yb[:, i]) for i in range(N_DIGITS)) / N_DIGITS
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 3.0)
            opt.step()
            train_loss += float(loss.item()) * xb.size(0)
            preds = torch.stack([logits[i].argmax(dim=1) for i in range(N_DIGITS)], dim=1)
            train_ok += int((preds == yb).all(dim=1).sum().item())
            train_n += xb.size(0)
        sched.step()

        model.eval()
        val_ok = val_n = 0
        val_loss = 0.0
        with torch.no_grad():
            for xb, yb in val_loader:
                xb = xb.to(device)
                yb = yb.to(device)
                logits = model(xb)
                loss = sum(criterion(logits[i], yb[:, i]) for i in range(N_DIGITS)) / N_DIGITS
                val_loss += float(loss.item()) * xb.size(0)
                preds = torch.stack([logits[i].argmax(dim=1) for i in range(N_DIGITS)], dim=1)
                val_ok += int((preds == yb).all(dim=1).sum().item())
                val_n += xb.size(0)

        all_ok, all_n = _eval_rows(model, all_rows, device=device)
        all_full = 100 * all_ok / max(all_n, 1)
        tr_full = 100 * train_ok / max(train_n, 1)
        va_full = 100 * val_ok / max(val_n, 1)
        if epoch == 1 or epoch % 10 == 0 or epoch == effective_epochs:
            print(
                f"[train-api] epoch {epoch}/{effective_epochs} "
                f"loss={train_loss / max(train_n, 1):.4f}/{val_loss / max(val_n, 1):.4f} "
                f"full={tr_full:.0f}%/{va_full:.0f}% all={all_full:.0f}%"
            )

        score = all_full if profile["pick_by"] == "all" else va_full
        if score > best_score:
            best_score = score
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            stale = 0
        else:
            stale += 1
            if stale >= train_patience and epoch > early_stop_after:
                print(f"[train-api] early stop на эпохе {epoch}")
                break

        if profile["pick_by"] == "all" and all_ok == all_n and epoch > 20:
            print(f"[train-api] 100% на всех {all_n} капчах — early stop на эпохе {epoch}")
            break

    if best_state:
        model.load_state_dict(best_state)

    all_full, all_n = _eval_rows(model, all_rows, device=device)
    save_model(model, out)

    # Метрики рядом с моделью. Без них единственный способ узнать, стала модель
    # лучше или хуже, — глазами в логе обучения, а лог живёт до перезапуска.
    accuracy = round(100 * all_full / max(all_n, 1), 2)
    save_metrics(
        {
            "trained_at": dt.datetime.now().isoformat(timespec="seconds"),
            "samples": all_n,
            "confirmed": confirmed_count(),
            "hard": hard_count(),
            "epochs_planned": effective_epochs,
            "epochs_done": epoch,
            "arch": arch,
            "best_val_percent": round(best_score, 2),
            "accuracy_percent": accuracy,
            "correct": all_full,
        },
        out,
    )
    print(
        f"[train-api] готово. best={best_score:.1f}% "
        f"all full={accuracy:.1f}% ({all_full}/{all_n})"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Обучить API-CNN (5 цифр)")
    ap.add_argument("--epochs", type=int, default=100)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--lr", type=float, default=0.0, help="0 = авто по размеру датасета")
    ap.add_argument("--min-samples", type=int, default=2)
    ap.add_argument("--patience", type=int, default=0, help="0 = авто")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument(
        "--arch",
        choices=("slice", "attn"),
        default="slice",
        help="slice — как браузерная captcha_model.pt (рекомендуется с pretrain)",
    )
    ap.add_argument(
        "--pretrain",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Взять backbone из airlines_web/captcha_model.pt (6 цифр, тот же стиль)",
    )
    ap.add_argument("--out", type=str, default="")
    ap.add_argument(
        "--from",
        dest="init_from",
        type=str,
        default="",
        help="Откуда взять веса для --resume (по умолчанию — файл из --out)",
    )
    args = ap.parse_args()
    out = Path(args.out) if args.out else None
    return train(
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr if args.lr > 0 else None,
        min_samples=args.min_samples,
        patience=args.patience if args.patience > 0 else None,
        resume=args.resume,
        init_from=Path(args.init_from) if args.init_from else None,
        arch=args.arch,
        pretrain=args.pretrain,
        out_path=out,
    )


if __name__ == "__main__":
    raise SystemExit(main())
