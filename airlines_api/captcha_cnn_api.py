"""Локальная CNN для API-капчи T5 iOS (5 цветных цифр, ~120×36)."""

from __future__ import annotations

import io
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
DEFAULT_MODEL_PATH = ROOT / "captcha_model_api.pt"
DEFAULT_BROWSER_MODEL_PATH = REPO_ROOT / "airlines_web" / "captcha_model.pt"

# API-капча ~120×36 — сохраняем пропорцию
IMG_H = 48
IMG_W = 160
N_DIGITS = 5
N_CLASSES = 10
N_CHANNELS = 3

_torch = None
_nn = None
_F = None
_model_cache = None


def _lazy_torch():
    global _torch, _nn, _F
    if _torch is None:
        import torch
        import torch.nn as nn
        import torch.nn.functional as F

        _torch = torch
        _nn = nn
        _F = F
    return _torch, _nn, _F


def model_path() -> Path:
    raw = (os.getenv("CAPTCHA_API_CNN_MODEL") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else ROOT / p
    return DEFAULT_MODEL_PATH


def browser_model_path() -> Path:
    """6-цифровая браузерная модель (тот же стиль капчи) для pretrain."""
    raw = (os.getenv("CAPTCHA_API_PRETRAIN_MODEL") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else REPO_ROOT / "airlines_web" / p
    return DEFAULT_BROWSER_MODEL_PATH


def init_from_browser_model(model, path: Path | None = None) -> tuple[int, int]:
    """Перенести веса из captcha_model.pt (backbone + совместимые головы)."""
    p = path or browser_model_path()
    if not p.exists():
        return 0, 0

    torch, _, _ = _lazy_torch()
    try:
        state = torch.load(p, map_location="cpu", weights_only=True)
    except TypeError:
        state = torch.load(p, map_location="cpu")

    loaded, skipped = load_matching_state(model, state)
    feat = sum(1 for k in loaded if k.startswith("features."))
    heads = sum(1 for k in loaded if k.startswith("heads."))
    other = len(loaded) - feat - heads
    print(
        f"[cnn-api] pretrain из {p.name}: "
        f"features={feat}, heads={heads}, other={other}, skip={len(skipped)}"
    )
    return len(loaded), len(skipped)


def min_confidence() -> float:
    try:
        return float((os.getenv("CAPTCHA_API_CNN_MIN_CONF") or "0.75").strip() or "0.75")
    except ValueError:
        return 0.75


def cnn_enabled() -> bool:
    return (os.getenv("CAPTCHA_API_USE_CNN") or "1").strip().lower() not in ("0", "false", "no")


def _build_feature_extractor(nn):
    return nn.Sequential(
        nn.Conv2d(N_CHANNELS, 32, 3, padding=1),
        nn.BatchNorm2d(32),
        nn.ReLU(inplace=True),
        nn.Conv2d(32, 32, 3, padding=1),
        nn.BatchNorm2d(32),
        nn.ReLU(inplace=True),
        nn.MaxPool2d(2),
        nn.Dropout2d(0.1),
        nn.Conv2d(32, 64, 3, padding=1),
        nn.BatchNorm2d(64),
        nn.ReLU(inplace=True),
        nn.Conv2d(64, 64, 3, padding=1),
        nn.BatchNorm2d(64),
        nn.ReLU(inplace=True),
        nn.MaxPool2d(2),
        nn.Dropout2d(0.15),
        nn.Conv2d(64, 128, 3, padding=1),
        nn.BatchNorm2d(128),
        nn.ReLU(inplace=True),
        nn.Conv2d(128, 128, 3, padding=1),
        nn.BatchNorm2d(128),
        nn.ReLU(inplace=True),
        nn.MaxPool2d(2),
        nn.Dropout2d(0.2),
        nn.Conv2d(128, 192, 3, padding=1),
        nn.BatchNorm2d(192),
        nn.ReLU(inplace=True),
    )


def build_model_slice():
    torch, nn, _ = _lazy_torch()

    class CaptchaCNNApiSlice(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.features = _build_feature_extractor(nn)
            self.pool = nn.AdaptiveAvgPool2d((5, 20))
            self.pos_w = 4
            self.heads = nn.ModuleList(
                [
                    nn.Sequential(
                        nn.Flatten(),
                        nn.Linear(192 * 5 * self.pos_w, 128),
                        nn.ReLU(inplace=True),
                        nn.Dropout(0.4),
                        nn.Linear(128, N_CLASSES),
                    )
                    for _ in range(N_DIGITS)
                ]
            )

        def forward(self, x):
            x = self.pool(self.features(x))
            outs = []
            for i, head in enumerate(self.heads):
                sl = x[:, :, :, i * self.pos_w : (i + 1) * self.pos_w]
                outs.append(head(sl))
            return outs

    return CaptchaCNNApiSlice()


def build_model():
    torch, nn, _ = _lazy_torch()
    feat_dim = 192

    class CaptchaCNNApi(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.features = _build_feature_extractor(nn)
            self.pool = nn.AdaptiveAvgPool2d((6, 28))
            self.pos_queries = nn.Parameter(torch.randn(N_DIGITS, feat_dim) * 0.02)
            self.pos_attn = nn.MultiheadAttention(
                feat_dim, num_heads=4, batch_first=True, dropout=0.1
            )
            self.pos_norm = nn.LayerNorm(feat_dim)
            self.heads = nn.ModuleList(
                [
                    nn.Sequential(
                        nn.Linear(feat_dim, 128),
                        nn.ReLU(inplace=True),
                        nn.Dropout(0.4),
                        nn.Linear(128, N_CLASSES),
                    )
                    for _ in range(N_DIGITS)
                ]
            )

        def forward(self, x):
            feat = self.pool(self.features(x))
            tokens = feat.flatten(2).transpose(1, 2)
            queries = self.pos_queries.unsqueeze(0).expand(feat.size(0), -1, -1)
            attended, _ = self.pos_attn(queries, tokens, tokens)
            attended = self.pos_norm(attended + queries)
            return [head(attended[:, i]) for i, head in enumerate(self.heads)]

    return CaptchaCNNApi()


def model_arch(state: dict) -> str:
    if "pos_queries" in state:
        return "attn"
    return "slice"


def build_model_for_state(state: dict):
    return build_model() if model_arch(state) == "attn" else build_model_slice()


def load_matching_state(model, state: dict) -> tuple[list[str], list[str]]:
    model_state = model.state_dict()
    loaded: dict = {}
    skipped: list[str] = []
    for key, value in state.items():
        if key not in model_state:
            skipped.append(key)
            continue
        if model_state[key].shape != value.shape:
            skipped.append(key)
            continue
        loaded[key] = value
    model.load_state_dict(loaded, strict=False)
    return sorted(loaded), skipped


def _pil_to_tensor(img):
    import numpy as np

    torch, _, _ = _lazy_torch()
    arr = np.asarray(img, dtype=np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std
    return torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)


def _augment_noise_lines(img) -> None:
    import math
    import random

    from PIL import ImageDraw

    w, h = img.size
    draw = ImageDraw.Draw(img)
    palette = [
        (28, 45, 110),
        (170, 35, 35),
        (195, 115, 55),
        (95, 55, 130),
        (45, 75, 95),
    ]
    span = max(w, h)
    for _ in range(random.randint(1, 4)):
        color = random.choice(palette)
        width = random.randint(1, 2)
        x1 = random.uniform(-0.15 * w, 1.15 * w)
        y1 = random.uniform(-0.15 * h, 1.15 * h)
        angle = math.radians(random.uniform(-65, 65))
        length = random.uniform(0.35, 1.15) * span
        x2 = x1 + length * math.cos(angle)
        y2 = y1 + length * math.sin(angle)
        draw.line([(x1, y1), (x2, y2)], fill=color, width=width)


def _augment_faint_digits(img, *, light: bool = False) -> None:
    """Симуляция бледных/полупрозрачных цифр в случайных позициях (1–5)."""
    import random

    import numpy as np
    from PIL import Image

    w, h = img.size
    arr = np.asarray(img, dtype=np.float32)
    col_w = w / N_DIGITS

    n_fade = 1 if light else random.randint(1, 2)
    positions = random.sample(range(N_DIGITS), min(n_fade, N_DIGITS))

    bg = 0.5 * np.median(arr[0:2, :, :], axis=(0, 1)) + 0.5 * np.median(arr[-2:, :, :], axis=(0, 1))

    for pos in positions:
        cx0 = pos * col_w
        cx1 = (pos + 1) * col_w
        jitter = random.uniform(-0.1, 0.12) * col_w
        x0 = max(0, int(cx0 + jitter))
        x1 = min(w, int(cx1 - jitter))
        if x1 - x0 < 4:
            continue

        strength = random.uniform(0.22, 0.52) if light else random.uniform(0.32, 0.78)
        strip_w = x1 - x0
        feather = max(2, strip_w // 5)
        mask = np.ones((h, strip_w), dtype=np.float32)
        for i in range(feather):
            t = (i + 1) / feather
            mask[:, i] = t
            mask[:, -(i + 1)] = t

        strip = arr[:, x0:x1, :].copy()
        gray = np.dot(strip, [0.299, 0.587, 0.114])[..., np.newaxis]
        desat = random.uniform(0.15, 0.45) if light else random.uniform(0.2, 0.65)
        strip = strip * (1.0 - desat) + gray * desat

        fade = strength * mask[..., np.newaxis]
        arr[:, x0:x1, :] = strip * (1.0 - fade) + bg * fade

    img.paste(Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)))


def preprocess_image(
    image_bytes: bytes,
    *,
    augment: bool = False,
    augment_light: bool = False,
):
    from PIL import Image, ImageEnhance, ImageOps
    import random

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    pad = max(1, min(w, h) // 40)
    img = img.crop((pad, pad, w - pad, h - pad))
    img = img.resize((IMG_W, IMG_H), Image.Resampling.BILINEAR)

    if augment:
        if augment_light:
            angle = random.uniform(-4, 4)
            scale = random.uniform(0.96, 1.04)
            dx = random.uniform(-0.02, 0.02) * IMG_W
            dy = random.uniform(-0.02, 0.02) * IMG_H
            noise_p = 0.35
            color_p = 0.45
            faint_p = 0.45
        else:
            angle = random.uniform(-8, 8)
            scale = random.uniform(0.92, 1.08)
            dx = random.uniform(-0.04, 0.04) * IMG_W
            dy = random.uniform(-0.04, 0.04) * IMG_H
            noise_p = 0.8
            color_p = 0.7
            faint_p = 0.65
        img = img.transform(
            (IMG_W, IMG_H),
            Image.Transform.AFFINE,
            (scale, 0, dx, 0, scale, dy),
            resample=Image.Resampling.BILINEAR,
            fillcolor=(200, 200, 200),
        )
        if abs(angle) > 0.5:
            img = img.rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=(200, 200, 200))
        if random.random() < noise_p:
            _augment_noise_lines(img)
        if random.random() < color_p:
            img = ImageEnhance.Color(img).enhance(random.uniform(0.8, 1.25))
        if random.random() < color_p:
            img = ImageEnhance.Contrast(img).enhance(random.uniform(0.85, 1.2))
        if random.random() < 0.4:
            img = ImageEnhance.Brightness(img).enhance(random.uniform(0.9, 1.1))
        if random.random() < 0.2:
            img = ImageOps.autocontrast(img)
        if random.random() < faint_p:
            _augment_faint_digits(img, light=augment_light)

    return _pil_to_tensor(img)


def label_to_tensor(code: str):
    torch, _, _ = _lazy_torch()
    digits = [int(c) for c in code if c.isdigit()]
    if len(digits) != N_DIGITS:
        raise ValueError(f"нужно {N_DIGITS} цифр, получено {code!r}")
    return torch.tensor(digits, dtype=torch.long)


def predict(image_bytes: bytes, *, model=None, path: Path | None = None) -> tuple[str, float]:
    torch, _, F = _lazy_torch()
    if model is None:
        model = load_model(path)
    if model is None:
        return "", 0.0

    model.eval()
    x = preprocess_image(image_bytes, augment=False)
    with torch.no_grad():
        logits = model(x)
        probs = [F.softmax(l, dim=1) for l in logits]
        digits = [int(p.argmax(dim=1).item()) for p in probs]
        confs = [float(p.max(dim=1).values.item()) for p in probs]
    code = "".join(str(d) for d in digits)
    conf = sum(confs) / len(confs) if confs else 0.0
    return code, conf


def load_model(path: Path | None = None):
    global _model_cache
    torch, _, _ = _lazy_torch()
    p = path or model_path()
    if not p.exists():
        return None
    if path is None and _model_cache is not None:
        return _model_cache
    try:
        try:
            state = torch.load(p, map_location="cpu", weights_only=True)
        except TypeError:
            state = torch.load(p, map_location="cpu")
        model = build_model_for_state(state)
        load_matching_state(model, state)
        model.eval()
        if path is None:
            _model_cache = model
        return model
    except Exception as exc:  # noqa: BLE001
        print(f"[cnn-api] не удалось загрузить {p.name}: {exc}")
        return None


def is_ready() -> bool:
    return cnn_enabled() and model_path().exists() and load_model() is not None


def try_solve(image_bytes: bytes) -> tuple[str, float] | None:
    if not is_ready():
        return None
    try:
        code, conf = predict(image_bytes)
    except Exception as exc:  # noqa: BLE001
        print(f"[cnn-api] ошибка predict: {exc}")
        return None
    if len(code) != N_DIGITS:
        print(f"[cnn-api] пропуск: code={code!r} len={len(code)}")
        return None
    return code, conf


def save_model(model, path: Path | None = None) -> Path:
    global _model_cache
    torch, _, _ = _lazy_torch()
    p = path or model_path()
    torch.save(model.state_dict(), p)
    _model_cache = None
    print(f"[cnn-api] модель сохранена: {p}")
    return p
