"""
core/story_designer.py — Automatically designs vertical (9:16) Instagram Stories.

Features:
  1. Creates 1080x1920 vertical canvas.
  2. Blurs + darkens original image for a rich, moody background.
  3. Applies a smooth gradient overlay (top and bottom fade to black).
  4. Overlays the main image with rounded corners and a soft glow border.
  5. Adds a sleek 'NEW POST' pill badge at the top.
  6. Adds a subtle decorative bottom bar.
"""

import os
import math
from PIL import Image, ImageFilter, ImageDraw, ImageFont


# ── Helpers ────────────────────────────────────────────────────────────────────

def _round_corners(img: Image.Image, radius: int) -> Image.Image:
    """Return image with rounded corners via an alpha mask."""
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([(0, 0), img.size], radius=radius, fill=255)
    img.putalpha(mask)
    return img


def _add_gradient_overlay(base: Image.Image) -> Image.Image:
    """
    Blend a top-to-bottom gradient over the base image.
    Top: black (70% opacity) → transparent → transparent → black (80% opacity) at bottom.
    """
    w, h = base.size
    gradient = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(gradient)

    # Top gradient — fades from solid black down to transparent over top 35%
    top_h = int(h * 0.38)
    for y in range(top_h):
        alpha = int(180 * (1 - y / top_h))
        draw.line([(0, y), (w, y)], fill=(0, 0, 0, alpha))

    # Bottom gradient — fades from transparent to solid black over bottom 30%
    bot_start = int(h * 0.68)
    for y in range(bot_start, h):
        alpha = int(200 * ((y - bot_start) / (h - bot_start)))
        draw.line([(0, y), (w, y)], fill=(0, 0, 0, alpha))

    return Image.alpha_composite(base.convert("RGBA"), gradient)


def _crisp_border_with_shadow(main_img: Image.Image, shadow_blur: int = 40) -> Image.Image:
    """
    Add a crisp white stroke and a deep drop shadow for a 3D aesthetic.
    """
    w, h = main_img.size
    pad = shadow_blur * 2
    expanded_w = w + pad * 2
    expanded_h = h + pad * 2

    # Canvas
    canvas = Image.new("RGBA", (expanded_w, expanded_h), (0, 0, 0, 0))
    
    # 1. Shadow layer
    shadow = Image.new("RGBA", (expanded_w, expanded_h), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        [(pad, pad + 15), (pad + w, pad + h + 15)], # slight downward offset
        radius=32,
        fill=(0, 0, 0, 180)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=shadow_blur))
    canvas = Image.alpha_composite(canvas, shadow)

    # 2. Main image
    canvas.paste(main_img, (pad, pad), main_img if main_img.mode == "RGBA" else None)

    # 3. Crisp white stroke
    stroke_overlay = Image.new("RGBA", (expanded_w, expanded_h), (0, 0, 0, 0))
    stroke_draw = ImageDraw.Draw(stroke_overlay)
    stroke_draw.rounded_rectangle(
        [(pad, pad), (pad + w, pad + h)],
        radius=32,
        outline=(255, 255, 255, 160),
        width=2
    )
    canvas = Image.alpha_composite(canvas, stroke_overlay)

    return canvas


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Try to load a nice system font, fall back gracefully."""
    candidates = []
    if bold:
        candidates = [
            "C:/Windows/Fonts/arialbd.ttf",       # Windows Arial Bold
            "C:/Windows/Fonts/calibrib.ttf",       # Windows Calibri Bold
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    else:
        candidates = [
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/calibri.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _draw_modern_badge(draw: ImageDraw.Draw, text: str, cx: int, cy: int, font: ImageFont.FreeTypeFont):
    """
    Draw a sleek, ultra-minimal badge with widely spaced text.
    Translucent dark pill with faint white border.
    """
    spaced_text = "   ".join(text.upper())
    bbox = draw.textbbox((0, 0), spaced_text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    pad_x, pad_y = 56, 24
    pill_w = tw + pad_x * 2
    pill_h = th + pad_y * 2
    x0 = cx - pill_w // 2
    y0 = cy - pill_h // 2
    x1 = cx + pill_w // 2
    y1 = cy + pill_h // 2

    # Translucent glass pill
    draw.rounded_rectangle([(x0, y0), (x1, y1)], radius=pill_h // 2, fill=(0, 0, 0, 110), outline=(255, 255, 255, 70), width=2)

    # Crisp white text
    tx = cx - tw // 2
    ty = y0 + pad_y - bbox[1]
    draw.text((tx, ty), spaced_text, font=font, fill=(255, 255, 255, 255))


def _draw_bottom_text(draw: ImageDraw.Draw, canvas_w: int, canvas_h: int, font: ImageFont.FreeTypeFont):
    """Elegant 'TAP TO VIEW' at the bottom."""
    text = "T A P   T O   V I E W"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    cx = canvas_w // 2
    tx = cx - tw // 2
    ty = canvas_h - 130
    
    draw.text((tx, ty), text, font=font, fill=(255, 255, 255, 180))
    
    # Tiny line underneath
    ly = ty + 50
    line_w = 40
    draw.line([(cx - line_w//2, ly), (cx + line_w//2, ly)], fill=(255, 255, 255, 140), width=2)


# ── Main Public Function ────────────────────────────────────────────────────────

def create_story_image(source_path: str, output_path: str, text: str = "NEW POST") -> bool:
    """
    Transform a standard image into a premium vertical Story (1080×1920).
    """
    try:
        with Image.open(source_path).convert("RGB") as img:
            canvas_w, canvas_h = 1080, 1920

            # ── A: Background ─────────────────────────────────────────────────
            img_w, img_h = img.size
            scale = max(canvas_w / img_w, canvas_h / img_h) * 1.05  # slight overscale for safety
            bg = img.resize((int(img_w * scale), int(img_h * scale)), Image.Resampling.LANCZOS)
            left = (bg.width - canvas_w) / 2
            top  = (bg.height - canvas_h) / 2
            bg = bg.crop((left, top, left + canvas_w, top + canvas_h))

            # Heavy blur for dreamy background
            bg = bg.filter(ImageFilter.GaussianBlur(radius=48))

            # Gentle saturation boost on background (vivid look)
            from PIL import ImageEnhance
            bg = ImageEnhance.Color(bg).enhance(1.6)
            bg = ImageEnhance.Brightness(bg).enhance(0.55)  # darken

            # Gradient overlay (top & bottom cinematic fade)
            bg = _add_gradient_overlay(bg)
            bg = bg.convert("RGB")

            # ── B: Main Image (rounded corners + glow) ─────────────────────────
            # Target width = 88% of canvas, maintain aspect ratio
            main_w = int(canvas_w * 0.88)
            main_h = int(img_h * (main_w / img_w))

            # Cap height so image doesn't overflow vertically
            max_main_h = int(canvas_h * 0.60)
            if main_h > max_main_h:
                main_h = max_main_h
                main_w = int(img_w * (main_h / img_h))

            main_img = img.resize((main_w, main_h), Image.Resampling.LANCZOS)

            # Rounded corners
            main_img = _round_corners(main_img, radius=32)

            # Crisp border + shadow
            main_with_fx = _crisp_border_with_shadow(main_img, shadow_blur=40)

            # Position: vertically centred, slightly above true centre for balance
            fx_w, fx_h = main_with_fx.size
            paste_x = (canvas_w - fx_w) // 2
            paste_y = (canvas_h - fx_h) // 2 + 50  # nudge down slightly

            # Composite onto background (supports RGBA)
            bg_rgba = bg.convert("RGBA")
            bg_rgba.paste(main_with_fx, (paste_x, paste_y), main_with_fx)
            bg = bg_rgba.convert("RGB")

            # ── C: Text & Badges ──────────────────────────────────────────────
            draw = ImageDraw.Draw(bg.convert("RGBA"))
            bg = bg.convert("RGBA")
            draw = ImageDraw.Draw(bg)

            # "NEW POST" sleek badge — positioned above the main image
            badge_font = _load_font(size=28, bold=True)
            badge_cy = paste_y - 80
            _draw_modern_badge(draw, text, canvas_w // 2, badge_cy, badge_font)

            # Elegant text at bottom
            bot_font = _load_font(size=24, bold=True)
            _draw_bottom_text(draw, canvas_w, canvas_h, bot_font)

            # ── D: Save ───────────────────────────────────────────────────────
            final = bg.convert("RGB")
            final.save(output_path, "JPEG", quality=97)
            return True

    except Exception as e:
        print(f"Failed to design story: {e}")
        return False


# ── Quick test (runs only when executed directly) ──────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3:
        ok = create_story_image(sys.argv[1], sys.argv[2])
        print("Done:" if ok else "Failed.", sys.argv[2])
    else:
        print("Usage: python story_designer.py <input_image> <output_image.jpg>")