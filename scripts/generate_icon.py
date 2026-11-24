"""Generate multi-resolution Windows .ico and iOS/PWA PNG icons for iPhone Mic Bridge."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT / "assets"
DOCS_DIR = ROOT / "docs"

SIZE = 1024


def _make_base_icon(transparent_bg: bool = True) -> Image.Image:
    # Radial dark-navy-to-indigo background gradient
    y, x = np.mgrid[0:SIZE, 0:SIZE].astype(np.float32)
    cx, cy = SIZE * 0.5, SIZE * 0.32
    dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / (SIZE * 0.85)
    t = np.clip(dist, 0.0, 1.0)

    r = (30 * (1 - t) + 10 * t).astype(np.uint8)
    g = (58 * (1 - t) + 16 * t).astype(np.uint8)
    b = (138 * (1 - t) + 36 * t).astype(np.uint8)
    a = np.full((SIZE, SIZE), 255, dtype=np.uint8)

    bg = Image.fromarray(np.dstack([r, g, b, a]), mode="RGBA")

    if transparent_bg:
        mask = Image.new("L", (SIZE, SIZE), 0)
        mdraw = ImageDraw.Draw(mask)
        pad = 48
        mdraw.rounded_rectangle([pad, pad, SIZE - pad, SIZE - pad], radius=216, fill=255)
        canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        canvas.paste(bg, (0, 0), mask)
    else:
        canvas = bg

    # Subtle blue glow behind microphone
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse([230, 170, 794, 734], fill=(59, 130, 246, 95))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=90))
    canvas = Image.alpha_composite(canvas, glow)

    # Outer border highlight on squircle
    if transparent_bg:
        border_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        bdraw = ImageDraw.Draw(border_layer)
        bdraw.rounded_rectangle([48, 48, SIZE - 48, SIZE - 48], radius=216, outline=(96, 165, 250, 95), width=10)
        canvas = Image.alpha_composite(canvas, border_layer)

    draw = ImageDraw.Draw(canvas)

    # Sound wave arcs (left and right of mic)
    wave_color_outer = (56, 189, 248, 130)
    wave_color_inner = (96, 165, 250, 210)
    for box, w, col in [
        ([188, 224, 412, 540], 26, wave_color_inner),
        ([112, 164, 444, 600], 22, wave_color_outer),
    ]:
        draw.arc(box, start=142, end=218, fill=col, width=w)
    for box, w, col in [
        ([612, 224, 836, 540], 26, wave_color_inner),
        ([580, 164, 912, 600], 22, wave_color_outer),
    ]:
        draw.arc(box, start=322, end=38, fill=col, width=w)

    # Microphone capsule (white with subtle cyan-blue bottom accent)
    cap_x0, cap_y0, cap_x1, cap_y1 = 412, 196, 612, 536
    draw.rounded_rectangle([cap_x0, cap_y0, cap_x1, cap_y1], radius=100, fill=(241, 245, 249, 255))

    # Subtle grille lines on capsule
    for gy in (290, 342, 394):
        draw.rounded_rectangle([456, gy, 568, gy + 16], radius=8, fill=(148, 163, 184, 150))

    # Microphone U-cradle arc
    cradle_box = [342, 326, 682, 636]
    draw.arc(cradle_box, start=0, end=180, fill=(96, 165, 250, 255), width=38)
    # Vertical ends of U-cradle
    draw.line([(361, 438), (361, 486)], fill=(96, 165, 250, 255), width=38)
    draw.line([(663, 438), (663, 486)], fill=(96, 165, 250, 255), width=38)
    draw.ellipse([342, 419, 380, 457], fill=(96, 165, 250, 255))
    draw.ellipse([644, 419, 682, 457], fill=(96, 165, 250, 255))

    # Stem and base stand
    draw.line([(512, 634), (512, 766)], fill=(219, 234, 254, 255), width=40)
    draw.rounded_rectangle([380, 748, 644, 796], radius=24, fill=(219, 234, 254, 255))

    # Live status dot (top-right inside squircle)
    draw.ellipse([746, 148, 826, 228], fill=(34, 197, 94, 255), outline=(15, 23, 42, 255), width=12)

    return canvas


def main() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    icon_trans = _make_base_icon(transparent_bg=True)
    icon_solid = _make_base_icon(transparent_bg=False)

    ico_sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    ico_path = ASSETS_DIR / "iphone-mic.ico"
    favicon_path = DOCS_DIR / "favicon.ico"

    icon_trans.save(ico_path, format="ICO", sizes=ico_sizes)
    icon_trans.save(favicon_path, format="ICO", sizes=ico_sizes)

    # Preview & PWA/iOS PNG icons
    preview_path = ASSETS_DIR / "icon-512.png"
    icon_trans.resize((512, 512), Image.Resampling.LANCZOS).save(preview_path, format="PNG")
    icon_solid.resize((512, 512), Image.Resampling.LANCZOS).save(DOCS_DIR / "icon-512.png", format="PNG")
    icon_solid.resize((192, 192), Image.Resampling.LANCZOS).save(DOCS_DIR / "icon-192.png", format="PNG")
    icon_solid.resize((180, 180), Image.Resampling.LANCZOS).save(DOCS_DIR / "apple-touch-icon.png", format="PNG")

    print("Generated:", ico_path, favicon_path, preview_path)


if __name__ == "__main__":
    main()
