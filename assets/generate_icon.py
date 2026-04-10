#!/usr/bin/env python3
"""Generate claude-manager icon assets."""
from PIL import Image, ImageDraw, ImageFont
import os

DIR = os.path.dirname(os.path.abspath(__file__))
SIZE = 512
BG = (13, 17, 23)        # #0d1117
ACCENT = (88, 166, 255)  # #58a6ff
TEXT = (230, 237, 243)    # #e6edf3

def make_icon(size=SIZE):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Circle background
    margin = int(size * 0.02)
    draw.ellipse([margin, margin, size - margin, size - margin], fill=BG, outline=ACCENT, width=max(2, size // 80))

    # Text "CM"
    font_size = int(size * 0.35)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except (OSError, IOError):
            try:
                font = ImageFont.truetype("C:\\Windows\\Fonts\\segoeui.ttf", font_size)
            except (OSError, IOError):
                font = ImageFont.load_default()

    text = "CM"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) // 2
    ty = (size - th) // 2 - bbox[1]  # Adjust for font baseline
    draw.text((tx, ty), text, fill=TEXT, font=font)

    return img

if __name__ == "__main__":
    icon = make_icon(512)

    # PNG (512x512)
    png_path = os.path.join(DIR, "icon.png")
    icon.save(png_path, "PNG")
    print(f"  Created: {png_path}")

    # ICO (multi-size for Windows) — downscale from 512px base to avoid tiny-font issues
    ico_path = os.path.join(DIR, "icon.ico")
    sizes = [16, 32, 48, 64, 128, 256]
    ico_images = [icon.resize((s, s), Image.LANCZOS) for s in sizes]
    ico_images[0].save(ico_path, format="ICO", sizes=[(s, s) for s in sizes], append_images=ico_images[1:])
    print(f"  Created: {ico_path}")

    # macOS .icns — needs iconutil, can't generate with Pillow
    # To create: save 512x512 PNG as icon.iconset/icon_512x512.png, then:
    #   iconutil -c icns icon.iconset -o icon.icns
    iconset_dir = os.path.join(DIR, "icon.iconset")
    os.makedirs(iconset_dir, exist_ok=True)
    for s in [16, 32, 64, 128, 256, 512]:
        # Always downscale from the 512px master to preserve quality
        resized = icon.resize((s, s), Image.LANCZOS)
        resized.save(os.path.join(iconset_dir, f"icon_{s}x{s}.png"))
        if s <= 256:
            doubled = icon.resize((s*2, s*2), Image.LANCZOS)
            doubled.save(os.path.join(iconset_dir, f"icon_{s}x{s}@2x.png"))
    print(f"  Created: {iconset_dir}/ (run 'iconutil -c icns {iconset_dir}' on macOS)")

    print("Done!")
