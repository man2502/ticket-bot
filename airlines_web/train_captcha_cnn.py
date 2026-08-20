#!/usr/bin/env python3
"""Обучение CNN: captcha_dataset/confirmed/ (easy) + captcha_dataset/hard/ (overlap)."""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from _bootstrap import WEB_ROOT, reexec_with_project_venv, setup_paths

reexec_with_project_venv()
setup_paths()

ROOT = WEB_ROOT

from captcha_cnn import (  # noqa: E402
    N_DIGITS,
    build_model,
    build_model_slice,
    label_to_tensor,
    load_matching_state,
    model_arch,
    model_path,
    preprocess_image,
    save_model,
)
from captcha_dataset import (  # noqa: E402
    confirmed_count,
    hard_count,
    list_training_samples,
)

# Позиции 5–6 (индексы 4,5) — зона overlap на T5
OVERLAP_POS_WEIGHT = 2.0
HARD_SAMPLE_WEIGHT = 1.5
ATTN_MODEL_PATH = ROOT / "captcha_model_attn.pt"


def _build_for_arch(arch: str):
    return build_model() if arch == "attn" else build_model_slice()


def _epoch_score(*, va_full: float, hard_val_full: float, va_digit: float) -> float:
    """Главное — val full (все капчи). Hard — вторичный бонус."""
    hard_bonus = max(hard_val_full, 0.0) * 10 if hard_val_full >= 0 else 0.0
    return va_full * 1000 + hard_bonus + va_digit


def _lazy_train_deps():
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, Dataset

    return torch, nn, DataLoader, Dataset


def _position_weights(device):
    import torch

    w = torch.ones(N_DIGITS, dtype=torch.float32, device=device)
    w[4] = OVERLAP_POS_WEIGHT
    w[5] = OVERLAP_POS_WEIGHT
    return w / w.sum() * N_DIGITS


def _expand_train_rows(
    train_rows: list[tuple[Path, str, bool]],
    *,
    hard_oversample: int,
) -> list[tuple[Path, str, bool]]:
    out: list[tuple[Path, str, bool]] = []
    extra = max(0, hard_oversample - 1)
    for path, code, hard in train_rows:
        out.append((path, code, hard))
        if hard:
            for _ in range(extra):
                out.append((path, code, True))
    return out


def _eval_rows(model, rows, *, device) -> tuple[int, int, int, int]:
    """full_ok, full_n, digit_ok, digit_n."""
    import torch

    full_ok = digit_ok = digit_n = 0
    with torch.no_grad():
        for path, code, _hard in rows:
            x = preprocess_image(path.read_bytes(), augment=False).to(device)
            logits = model(x)
            pred = "".join(str(int(l.argmax(dim=1).item())) for l in logits)
            full_ok += int(pred == code)
            for a, b in zip(pred, code):
                digit_ok += int(a == b)
                digit_n += 1
    return full_ok, len(rows), digit_ok, digit_n


def _weighted_loss(criterion, logits, yb, sample_weights, pos_weights):
    import torch

    per_sample = torch.zeros(yb.size(0), device=yb.device)
    for i in range(N_DIGITS):
        ce = criterion(logits[i], yb[:, i])
        per_sample += ce * pos_weights[i]
    per_sample *= sample_weights
    return per_sample.mean()


def train(
    *,
    epochs: int = 120,
    batch_size: int = 8,
    lr: float = 8e-4,
    val_ratio: float = 0.2,
    min_samples: int = 20,
    out_path: Path | None = None,
    patience: int = 25,
    resume: bool = False,
    hard_oversample: int = 4,
    arch: str = "slice",
) -> int:
    torch, nn, DataLoader, Dataset = _lazy_train_deps()

    all_rows = list_training_samples()
    easy_n = confirmed_count()
    hard_n = hard_count()
    total_n = len(all_rows)

    if total_n < min_samples:
        print(
            f"[train] мало данных: {total_n} < {min_samples} "
            f"(confirmed/={easy_n}, hard/={hard_n}). "
            f"Бот → confirmed/, label_rejected.py → hard/"
        )
        return 1

    random.seed(42)
    shuffled = all_rows[:]
    random.shuffle(shuffled)
    n_val = max(2, int(len(shuffled) * val_ratio))
    n_val = min(n_val, max(2, len(shuffled) // 5))
    val_rows = shuffled[:n_val]
    train_rows_raw = shuffled[n_val:]
    if len(train_rows_raw) < 8:
        train_rows_raw, val_rows = shuffled, shuffled[: max(2, len(shuffled) // 5)]

    train_rows = _expand_train_rows(train_rows_raw, hard_oversample=hard_oversample)
    val_hard = [r for r in val_rows if r[2]]
    train_hard = [r for r in train_rows_raw if r[2]]
    hard_all = [r for r in all_rows if r[2]]
    easy_all = [r for r in all_rows if not r[2]]

    print(
        f"[train] confirmed/={easy_n} hard/={hard_n} total={total_n} | "
        f"train={len(train_rows_raw)} val={len(val_rows)} | RGB + аугментации"
    )
    print(
        f"[train] hard train={len(train_hard)} val={len(val_hard)} | "
        f"oversample×{hard_oversample} pos4-5×{OVERLAP_POS_WEIGHT} | "
        f"train строк с дублями={len(train_rows)}"
    )
    if not hard_n:
        print("[train] hard/ пуст — разметь rejected: .venv/bin/python label_rejected.py")

    class _DS(Dataset):
        def __init__(self, rows, augment: bool):
            self.rows = rows
            self.augment = augment

        def __len__(self):
            return len(self.rows)

        def __getitem__(self, i):
            import torch

            path, code, hard = self.rows[i]
            x = preprocess_image(path.read_bytes(), augment=self.augment).squeeze(0)
            y = label_to_tensor(code)
            w = HARD_SAMPLE_WEIGHT if hard else 1.0
            return x, y, torch.tensor(w, dtype=torch.float32)

    device = torch.device("cpu")
    pos_weights = _position_weights(device)

    train_loader = DataLoader(
        _DS(train_rows, augment=True),
        batch_size=min(batch_size, len(train_rows)),
        shuffle=True,
        drop_last=False,
    )
    val_loader = DataLoader(
        _DS(val_rows, augment=False),
        batch_size=min(batch_size, len(val_rows)),
        shuffle=False,
    )

    train_arch = arch
    resume_from = out_path or model_path()
    baseline_full: int | None = None
    baseline_n = 0
    if arch == "attn" and out_path is None and resume_from == model_path():
        out = ATTN_MODEL_PATH
        print(
            f"[train] arch=attn → сохранение в {out.name} "
            f"(основная {model_path().name} не перезаписывается)"
        )
    else:
        out = resume_from

    if resume and resume_from.exists():
        torch, _, _, _ = _lazy_train_deps()
        try:
            state = torch.load(resume_from, map_location="cpu", weights_only=True)
        except TypeError:
            state = torch.load(resume_from, map_location="cpu")
        ckpt_arch = model_arch(state)
        if arch != ckpt_arch and arch == "attn":
            model = _build_for_arch("attn")
            loaded, skipped = load_matching_state(model, state)
            print(
                f"[train] миграция {ckpt_arch}→attn из {resume_from.name}: "
                f"backbone {len(loaded)} слоёв, attention-головы с нуля"
            )
        else:
            train_arch = ckpt_arch
            model = _build_for_arch(train_arch)
            loaded, skipped = load_matching_state(model, state)
            print(f"[train] fine-tune {train_arch} из {resume_from.name} ({len(loaded)} слоёв)")
        if out == model_path():
            model.eval()
            baseline_full, baseline_n, _, _ = _eval_rows(model, all_rows, device="cpu")
            baseline_pct = 100 * baseline_full / max(baseline_n, 1)
            print(f"[train] baseline all full={baseline_pct:.1f}% ({baseline_full}/{baseline_n})")
    else:
        model = _build_for_arch(train_arch)
        if resume:
            print(f"[train] --resume: {resume_from.name} не найден, обучение {train_arch} с нуля")
        else:
            print(f"[train] обучение {train_arch} с нуля")

    model.to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-3)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs, eta_min=lr * 0.05)
    criterion = nn.CrossEntropyLoss(reduction="none", label_smoothing=0.05)

    best_score = -1.0
    best_state = None
    stale = 0

    for epoch in range(1, epochs + 1):
        model.train()
        train_loss = 0.0
        train_digit_ok = 0
        train_digit_n = 0
        train_full_ok = 0
        train_n = 0
        for xb, yb, wb in train_loader:
            xb = xb.to(device)
            yb = yb.to(device)
            wb = wb.to(device)
            opt.zero_grad()
            logits = model(xb)
            loss = _weighted_loss(criterion, logits, yb, wb, pos_weights)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 3.0)
            opt.step()
            train_loss += float(loss.item()) * xb.size(0)
            preds = torch.stack([logits[i].argmax(dim=1) for i in range(N_DIGITS)], dim=1)
            train_digit_ok += int((preds == yb).sum().item())
            train_digit_n += yb.numel()
            train_full_ok += int((preds == yb).all(dim=1).sum().item())
            train_n += xb.size(0)
        sched.step()

        model.eval()
        val_loss = 0.0
        val_digit_ok = 0
        val_digit_n = 0
        val_full_ok = 0
        val_n = 0
        with torch.no_grad():
            for xb, yb, wb in val_loader:
                xb = xb.to(device)
                yb = yb.to(device)
                wb = wb.to(device)
                logits = model(xb)
                loss = _weighted_loss(criterion, logits, yb, wb, pos_weights)
                val_loss += float(loss.item()) * xb.size(0)
                preds = torch.stack([logits[i].argmax(dim=1) for i in range(N_DIGITS)], dim=1)
                val_digit_ok += int((preds == yb).sum().item())
                val_digit_n += yb.numel()
                val_full_ok += int((preds == yb).all(dim=1).sum().item())
                val_n += xb.size(0)

        hard_val_full = -1.0
        if val_hard:
            h_full, h_n, _, _ = _eval_rows(model, val_hard, device=device)
            hard_val_full = 100 * h_full / max(h_n, 1)

        tr_loss = train_loss / max(train_n, 1)
        va_loss = val_loss / max(val_n, 1)
        tr_digit = train_digit_ok / max(train_digit_n, 1) * 100
        va_digit = val_digit_ok / max(val_digit_n, 1) * 100
        tr_full = train_full_ok / max(train_n, 1) * 100
        va_full = val_full_ok / max(val_n, 1) * 100

        if epoch == 1 or epoch % 5 == 0 or epoch == epochs:
            hard_line = f" hard_val={hard_val_full:.0f}%" if val_hard else ""
            print(
                f"[train] epoch {epoch}/{epochs} "
                f"loss={tr_loss:.4f}/{va_loss:.4f} "
                f"digit={tr_digit:.0f}%/{va_digit:.0f}% "
                f"full={tr_full:.0f}%/{va_full:.0f}%{hard_line}"
            )

        score = _epoch_score(
            va_full=va_full,
            hard_val_full=hard_val_full,
            va_digit=va_digit,
        )
        if score > best_score + 0.05:
            best_score = score
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            stale = 0
        else:
            stale += 1
            if stale >= patience and epoch > 40:
                print(f"[train] early stop на эпохе {epoch} (patience={patience})")
                break

    if best_state:
        model.load_state_dict(best_state)

    model.eval()
    train_full, train_n, _, _ = _eval_rows(model, train_rows_raw, device=device)
    val_full, val_n, _, _ = _eval_rows(model, val_rows, device=device)
    all_full, all_n, _, _ = _eval_rows(model, all_rows, device=device)
    easy_full = easy_digit = easy_digit_n = 0
    if easy_all:
        easy_full, easy_n_eval, easy_digit, easy_digit_n = _eval_rows(
            model, easy_all, device=device
        )
        easy_report = (
            f"easy={100 * easy_full / max(easy_n_eval, 1):.1f}% "
            f"({easy_full}/{easy_n_eval})"
        )
    else:
        easy_report = "easy=—"
    if hard_all:
        hard_full, hard_n_eval, hard_digit, hard_digit_n = _eval_rows(
            model, hard_all, device=device
        )
        hard_report = (
            f"hard={100 * hard_full / max(hard_n_eval, 1):.1f}% "
            f"({hard_full}/{hard_n_eval}) digit={100 * hard_digit / max(hard_digit_n, 1):.1f}%"
        )
    else:
        hard_report = "hard=—"

    prod = model_path()
    if out == prod and baseline_full is not None:
        baseline_pct = 100 * baseline_full / max(baseline_n, 1)
        new_pct = 100 * all_full / max(all_n, 1)
        if new_pct + 1.0 < baseline_pct:
            alt = out.with_name(out.stem + "_candidate.pt")
            save_model(model, alt)
            print(
                f"[train] ⚠ новая модель хуже ({new_pct:.1f}% vs {baseline_pct:.1f}% all) — "
                f"сохранено в {alt.name}, {prod.name} не тронут"
            )
            return 1
    save_model(model, out)

    print(
        f"[train] готово ({train_arch}). best score={best_score:.1f} | "
        f"train full={100 * train_full / max(train_n, 1):.1f}% "
        f"val full={100 * val_full / max(val_n, 1):.1f}% "
        f"all full={100 * all_full / max(all_n, 1):.1f}% | "
        f"{easy_report} | {hard_report}"
    )
    if hard_all and hard_full < hard_n_eval:
        print(
            "[train] ⚠ ошибки на hard/ — разметь ещё rejected и переобучи с --resume:\n"
            "  .venv/bin/python label_rejected.py\n"
            "  .venv/bin/python train_captcha_cnn.py --resume"
        )
    elif best_score < 40:
        print("[train] ⚠ точность низкая — копи confirmed/ ботом или разметь rejected")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Обучить CNN: captcha_dataset/confirmed/ + captcha_dataset/hard/"
    )
    ap.add_argument("--epochs", type=int, default=120)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--lr", type=float, default=8e-4)
    ap.add_argument("--min-samples", type=int, default=20)
    ap.add_argument("--patience", type=int, default=25)
    ap.add_argument(
        "--resume",
        action="store_true",
        help="Fine-tune из captcha_model.pt вместо обучения с нуля",
    )
    ap.add_argument(
        "--hard-oversample",
        type=int,
        default=4,
        help="Сколько раз hard/ капча встречается в train за эпоху (default 4)",
    )
    ap.add_argument(
        "--arch",
        choices=("slice", "attn"),
        default="slice",
        help="slice — текущая prod-модель; attn — эксперимент (→ captcha_model_attn.pt)",
    )
    ap.add_argument("--out", type=str, default="")
    args = ap.parse_args()
    out = Path(args.out) if args.out else None
    return train(
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        min_samples=args.min_samples,
        out_path=out,
        patience=args.patience,
        resume=args.resume,
        hard_oversample=max(1, args.hard_oversample),
        arch=args.arch,
    )


if __name__ == "__main__":
    raise SystemExit(main())
