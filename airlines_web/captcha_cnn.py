"""Локальная CNN для капчи T5 (6 цветных цифр с наложением)."""

from __future__ import annotations

import io
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL_PATH = ROOT / "captcha_model.pt"

# Оригинал ~321×151 — держим пропорцию и цвет (цвет критичен: цифры разных цветов)
IMG_H = 80
IMG_W = 192
N_DIGITS = 6
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
    raw = (os.getenv("CAPTCHA_CNN_MODEL") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else ROOT / p
    return DEFAULT_MODEL_PATH


def min_confidence() -> float:
    try:
        return float((os.getenv("CAPTCHA_CNN_MIN_CONF") or "0.82").strip() or "0.82")
    except ValueError:
        return 0.82


def cnn_enabled() -> bool:
    return (os.getenv("CAPTCHA_USE_CNN") or "1").strip().lower() not in ("0", "false", "no")


def _build_feature_extractor(nn):
    """Общий RGB-backbone для всех архитектур."""
    return nn.Sequential(
        nn.Conv2d(N_CHANNELS, 32, 3, padding=1),
        nn.BatchNorm2d(32),
        nn.ReLU(inplace=True),
        nn.Conv2d(32, 32, 3, padding=1),
        nn.BatchNorm2d(32),
        nn.ReLU(inplace=True),
        nn.MaxPool2d(2),  # 40×96
        nn.Dropout2d(0.1),
        nn.Conv2d(32, 64, 3, padding=1),
        nn.BatchNorm2d(64),
        nn.ReLU(inplace=True),
        nn.Conv2d(64, 64, 3, padding=1),
        nn.BatchNorm2d(64),
        nn.ReLU(inplace=True),
        nn.MaxPool2d(2),  # 20×48
        nn.Dropout2d(0.15),
        nn.Conv2d(64, 128, 3, padding=1),
        nn.BatchNorm2d(128),
        nn.ReLU(inplace=True),
        nn.Conv2d(128, 128, 3, padding=1),
        nn.BatchNorm2d(128),
        nn.ReLU(inplace=True),
        nn.MaxPool2d(2),  # 10×24
        nn.Dropout2d(0.2),
        nn.Conv2d(128, 192, 3, padding=1),
        nn.BatchNorm2d(192),
        nn.ReLU(inplace=True),
    )


def build_model_slice():
    """
    Старая архитектура: 6 равных вертикальных полос (ломается на overlap).
    Оставлена для загрузки captcha_model.pt v1.
    """
    torch, nn, _ = _lazy_torch()

    class CaptchaCNNSlice(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.features = _build_feature_extractor(nn)
            self.pool = nn.AdaptiveAvgPool2d((6, 24))
            self.pos_w = 4
            self.heads = nn.ModuleList(
                [
                    nn.Sequential(
                        nn.Flatten(),
                        nn.Linear(192 * 6 * self.pos_w, 128),
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

    return CaptchaCNNSlice()


def build_model():
    """
    RGB-CNN + attention по позициям.
    Каждая из 6 «голов» сама выбирает, куда смотреть на карте признаков —
    не привязана к равному делению по ширине (overlap, разный размер цифр).
    """
    torch, nn, _ = _lazy_torch()
    feat_dim = 192

    class CaptchaCNN(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.features = _build_feature_extractor(nn)
            self.pool = nn.AdaptiveAvgPool2d((8, 32))
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
            feat = self.pool(self.features(x))  # [B, 192, 8, 32]
            tokens = feat.flatten(2).transpose(1, 2)  # [B, H*W, C]
            queries = self.pos_queries.unsqueeze(0).expand(feat.size(0), -1, -1)
            attended, _ = self.pos_attn(queries, tokens, tokens)
            attended = self.pos_norm(attended + queries)
            return [head(attended[:, i]) for i, head in enumerate(self.heads)]

    return CaptchaCNN()


def model_arch(state: dict) -> str:
    if "pos_queries" in state:
        return "attn"
    return "slice"


def build_model_for_state(state: dict):
    return build_model() if model_arch(state) == "attn" else build_model_slice()


def load_matching_state(model, state: dict) -> tuple[list[str], list[str]]:
    """Загрузить совместимые веса; вернуть (загружено, пропущено)."""
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
    """→ torch.Tensor 1CHW. Аннотацию не ставим: torch грузится лениво (см. _lazy_torch)."""
    import numpy as np

    torch, _, _ = _lazy_torch()
    arr = np.asarray(img, dtype=np.float32) / 255.0  # HWC RGB
    # ImageNet-ish normalize for RGB
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std
    t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)  # 1CHW
    return t


def _augment_noise_lines(img) -> None:
    """Тонкие диагональные линии как на капче T5 (in-place)."""
    import math
    import random

    from PIL import ImageDraw

    w, h = img.size
    draw = ImageDraw.Draw(img)
    # типичные цвета шума на T5
    palette = [
        (28, 45, 110),  # тёмно-синий
        (170, 35, 35),  # красный
        (195, 115, 55),  # оранжево-коричневый
        (95, 55, 130),  # фиолетовый
        (45, 75, 95),  # сине-зелёный
    ]
    n_lines = random.randint(1, 4)
    span = max(w, h)
    for _ in range(n_lines):
        color = random.choice(palette)
        width = random.randint(1, 2)
        x1 = random.uniform(-0.15 * w, 1.15 * w)
        y1 = random.uniform(-0.15 * h, 1.15 * h)
        angle = math.radians(random.uniform(-65, 65))
        length = random.uniform(0.35, 1.15) * span
        x2 = x1 + length * math.cos(angle)
        y2 = y1 + length * math.sin(angle)
        draw.line([(x1, y1), (x2, y2)], fill=color, width=width)
        # иногда короткий отрезок рядом (как на 2-й капче)
        if random.random() < 0.35:
            sx = random.uniform(0, w)
            sy = random.uniform(0, h)
            seg = random.uniform(0.08, 0.22) * span
            sa = math.radians(random.uniform(-80, 80))
            draw.line(
                [(sx, sy), (sx + seg * math.cos(sa), sy + seg * math.sin(sa))],
                fill=random.choice(palette),
                width=width,
            )


def preprocess_image(image_bytes: bytes, *, augment: bool = False):
    """PNG bytes → tensor [1, 3, H, W]."""
    from PIL import Image, ImageEnhance, ImageOps
    import random

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    # чуть обрезаем края (часто пустая рамка вокруг капчи)
    w, h = img.size
    pad = max(2, min(w, h) // 40)
    img = img.crop((pad, pad, w - pad, h - pad))
    img = img.resize((IMG_W, IMG_H), Image.Resampling.BILINEAR)

    if augment:
        # лёгкий сдвиг / поворот / масштаб
        angle = random.uniform(-8, 8)
        scale = random.uniform(0.92, 1.08)
        dx = random.uniform(-0.04, 0.04) * IMG_W
        dy = random.uniform(-0.04, 0.04) * IMG_H
        img = img.transform(
            (IMG_W, IMG_H),
            Image.Transform.AFFINE,
            (scale, 0, dx, 0, scale, dy),
            resample=Image.Resampling.BILINEAR,
            fillcolor=(200, 200, 200),
        )
        if abs(angle) > 0.5:
            img = img.rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=(200, 200, 200))
        # шумовые линии — почти всегда есть на реальной капче
        if random.random() < 0.8:
            _augment_noise_lines(img)
        # цвет / контраст / яркость
        if random.random() < 0.7:
            img = ImageEnhance.Color(img).enhance(random.uniform(0.7, 1.4))
        if random.random() < 0.7:
            img = ImageEnhance.Contrast(img).enhance(random.uniform(0.75, 1.35))
        if random.random() < 0.5:
            img = ImageEnhance.Brightness(img).enhance(random.uniform(0.85, 1.2))
        if random.random() < 0.3:
            img = ImageOps.autocontrast(img)

    return _pil_to_tensor(img)


def label_to_tensor(code: str):
    torch, _, _ = _lazy_torch()
    digits = [int(c) for c in code if c.isdigit()]
    if len(digits) != N_DIGITS:
        raise ValueError(f"нужно {N_DIGITS} цифр, получено {code!r}")
    return torch.tensor(digits, dtype=torch.long)


def predict(image_bytes: bytes, *, model=None, path: Path | None = None) -> tuple[str, float]:
    """Предсказание 6 цифр + средняя уверенность."""
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
    # кэш: не перезагружать на каждый вызов
    if path is None and _model_cache is not None:
        return _model_cache
    try:
        try:
            state = torch.load(p, map_location="cpu", weights_only=True)
        except TypeError:
            state = torch.load(p, map_location="cpu")
        # старая grayscale-модель несовместима — игнорируем
        if any(k.startswith("features.0.weight") for k in state):
            w0 = state.get("features.0.weight")
            if w0 is not None and w0.shape[1] != N_CHANNELS:
                print(
                    f"[cnn] старая модель (каналы={w0.shape[1]}) несовместима с RGB — "
                    f"переобучи: .venv/bin/python train_captcha_cnn.py"
                )
                return None
        model = build_model_for_state(state)
        loaded, skipped = load_matching_state(model, state)
        if skipped and model_arch(state) == "slice" and "pos_queries" not in state:
            print(
                "[cnn] загружена slice-модель (равное деление на 6). "
                "Для overlap лучше: train_captcha_cnn.py --resume"
            )
        elif skipped:
            head_skip = [k for k in skipped if k.startswith("heads.")]
            if head_skip:
                print(f"[cnn] частичная загрузка: backbone ok, новые головы ({len(head_skip)} слоёв)")
        model.eval()
        if path is None:
            _model_cache = model
        return model
    except Exception as exc:  # noqa: BLE001
        print(f"[cnn] не удалось загрузить модель {p.name}: {exc}")
        return None


def is_ready() -> bool:
    return cnn_enabled() and model_path().exists() and load_model() is not None


def try_solve(image_bytes: bytes) -> tuple[str, float] | None:
    """CNN-ответ если модель есть и уверенность достаточна."""
    if not is_ready():
        return None
    try:
        code, conf = predict(image_bytes)
    except Exception as exc:  # noqa: BLE001
        print(f"[cnn] ошибка predict: {exc}")
        return None
    if len(code) != N_DIGITS or conf < min_confidence():
        print(f"[cnn] пропуск: code={code!r} conf={conf:.2f} < {min_confidence():.2f}")
        return None
    print(f"[cnn] ответ: {code} (conf={conf:.2f})")
    return code, conf


def save_model(model, path: Path | None = None) -> Path:
    global _model_cache
    torch, _, _ = _lazy_torch()
    p = path or model_path()
    torch.save(model.state_dict(), p)
    _model_cache = None
    print(f"[cnn] модель сохранена: {p}")
    return p
