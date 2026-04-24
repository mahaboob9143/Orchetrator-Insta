"""
core/story_designer.py — Automatically designs vertical (9:16) Instagram Stories.

Features:
  1. Creates 1080x1920 vertical canvas.
  2. Blurs the original image for a premium background effect.
  3. Overlays the crisp original image in the center.
  4. Adds 'NEW POST' branding text.
"""

import os
from PIL import Image, ImageFilter, ImageDraw, ImageFont

def create_story_image(source_path: str, output_path: str, text: str = "NEW POST"):
    """
    Transform a standard image into a premium vertical Story.
    """
    try:
        # 1. Open source and prepare canvas
        with Image.open(source_path).convert("RGB") as img:
            canvas_w, canvas_h = 1080, 1920
            
            # ── Step A: Background (Blurred) ──────────────────────────────────
            # Scale image to cover the whole canvas
            img_w, img_h = img.size
            scale_factor = max(canvas_w / img_w, canvas_h / img_h)
            bg_size = (int(img_w * scale_factor), int(img_h * scale_factor))
            
            background = img.resize(bg_size, Image.Resampling.LANCZOS)
            # Crop to exact canvas size
            left = (background.width - canvas_w) / 2
            top = (background.height - canvas_h) / 2
            background = background.crop((left, top, left + canvas_w, top + canvas_h))
            
            # Apply heavy blur + darkening
            background = background.filter(ImageFilter.GaussianBlur(radius=40))
            overlay = Image.new('RGB', background.size, (0, 0, 0))
            background = Image.blend(background, overlay, 0.3)  # 30% black tint
            
            # ── Step B: Main Image (Center) ───────────────────────────────
            # Scale to fits width with some padding
            main_w = int(canvas_w * 0.85)
            main_h = int(img_h * (main_w / img_w))
            
            main_img = img.resize((main_w, main_h), Image.Resampling.LANCZOS)
            
            # Add white border
            border_px = 4
            bordered_main = Image.new('RGB', (main_w + border_px*2, main_h + border_px*2), (255, 255, 255))
            bordered_main.paste(main_img, (border_px, border_px))
            
            # Paste in center
            center_y = (canvas_h - main_h) // 2
            background.paste(bordered_main, ((canvas_w - main_w) // 2 - border_px, center_y))
            
            # ── Step C: Text Overlay ──────────────────────────────────────────
            draw = ImageDraw.Draw(background)
            
            # Try to find a system font, otherwise default
            font = None
            font_paths = [
                "C:/Windows/Fonts/arial.ttf",  # Windows
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", # Linux
                "/System/Library/Fonts/Helvetica.ttc", # Mac
            ]
            
            for path in font_paths:
                if os.path.exists(path):
                    try:
                        font = ImageFont.truetype(path, 80)
                        break
                    except:
                        continue
            
            if not font:
                font = ImageFont.load_default()
            
            # Draw 'NEW POST'
            text_w = 400 # rough estimate if default
            try:
                # Get text bounding box for newer PIL versions
                left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
                text_w = right - left
            except:
                pass
            
            draw.text(((canvas_w - text_w) // 2, center_y - 150), text, font=font, fill=(255, 255, 255))
            
            # Save
            background.save(output_path, "JPEG", quality=95)
            return True
            
    except Exception as e:
        print(f"Failed to design story: {e}")
        return False
