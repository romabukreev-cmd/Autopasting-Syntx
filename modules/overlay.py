import io
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

FONTS_DIR = Path(__file__).parent.parent / "fonts"

# Font paths relative to fonts/
FONT_SF_LIGHT_ITALIC = "San Francisco Pro Display/SF-Pro-Display-LightItalic.otf"
FONT_SF_SEMIBOLD = "San Francisco Pro Display/SF-Pro-Display-Semibold.otf"
FONT_SF_REGULAR = "San Francisco Pro Display/SF-Pro-Display-Regular.otf"
FONT_JOYSTIX = "Joystix/joystix monospace.ttf"


@dataclass
class OverlayConfig:
    design_width: int      # Figma canvas width — all px values are relative to this
    margin: int            # safe zone on all edges (px)
    gradient_height: int   # height of bottom gradient rectangle (px)
    font_size_top: int     # SF Pro: top 2-line handle block
    font_size_title: int   # JoyStix: /PROMPT label
    font_size_prompt: int  # SF Pro Regular: prompt body text
    prompt_gap: int        # gap between /PROMPT label and prompt text (px)
    top_line_gap: int      # gap between the 2 top lines (px)


NANOBANA_CONFIG = OverlayConfig(
    design_width=848,
    margin=25,
    gradient_height=270,
    font_size_top=20,
    font_size_title=60,
    font_size_prompt=15,
    prompt_gap=15,
    top_line_gap=4,
)

_CONFIGS = {
    "nanobana": NANOBANA_CONFIG,
}


def _load_font(filename: str, size: int) -> ImageFont.FreeTypeFont:
    path = FONTS_DIR / filename
    try:
        return ImageFont.truetype(str(path), size)
    except Exception as e:
        logger.warning("Font %s not found (%s), falling back to default", filename, e)
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def _region_brightness(img: Image.Image, y_start: int, y_end: int) -> str:
    """Returns 'light' if the region avg brightness > 160, else 'dark'."""
    w = img.width
    crop = img.crop((0, y_start, w, y_end)).convert("L")
    pixels = list(crop.getdata())
    avg = sum(pixels) / len(pixels) if pixels else 0
    return "light" if avg > 160 else "dark"


def _make_gradient(width: int, height: int, gradient_height: int) -> Image.Image:
    """Full-size RGBA image: black gradient at bottom, transparent above."""
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    top_y = height - gradient_height
    for y in range(gradient_height):
        # y=0 → transparent (alpha=0), y=gradient_height-1 → opaque (alpha=255)
        alpha = int(255 * y / max(gradient_height - 1, 1))
        draw.line([(0, top_y + y), (width - 1, top_y + y)], fill=(0, 0, 0, alpha))
    return overlay


def _wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] > max_width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _line_height(font: ImageFont.FreeTypeFont, draw: ImageDraw.ImageDraw) -> int:
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    return bbox[3] - bbox[1]


def _draw_justified(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    x: int,
    y: int,
    max_width: int,
    line_h: int,
    fill: tuple,
) -> None:
    """Draw lines of text with justified alignment (last line left-aligned)."""
    for i, line in enumerate(lines):
        is_last = i == len(lines) - 1
        words = line.split()

        if is_last or len(words) <= 1:
            draw.text((x, y), line, font=font, fill=fill)
        else:
            word_widths = [
                draw.textbbox((0, 0), w, font=font)[2] - draw.textbbox((0, 0), w, font=font)[0]
                for w in words
            ]
            total_word_w = sum(word_widths)
            extra = (max_width - total_word_w) / (len(words) - 1)
            cx = float(x)
            for j, (word, ww) in enumerate(zip(words, word_widths)):
                draw.text((int(cx), y), word, font=font, fill=fill)
                cx += ww + extra

        y += line_h


def apply_overlay(image_data: bytes, prompt: str, model_type: str) -> bytes:
    """
    Apply design overlay to generated image.
    Returns JPEG bytes.
    """
    cfg = _CONFIGS.get(model_type)
    if cfg is None:
        raise ValueError(f"Unknown model_type: {model_type!r}")

    img = Image.open(io.BytesIO(image_data)).convert("RGBA")
    w, h = img.size

    # Scale all Figma pixel values proportionally to actual image size
    scale = w / cfg.design_width
    margin = round(cfg.margin * scale)
    gradient_height = round(cfg.gradient_height * scale)
    font_size_top = round(cfg.font_size_top * scale)
    font_size_title = round(cfg.font_size_title * scale)
    font_size_prompt = round(cfg.font_size_prompt * scale)
    prompt_gap = round(cfg.prompt_gap * scale)
    top_line_gap = round(cfg.top_line_gap * scale)

    # --- Auto text color: analyze top and bottom zones ---
    top_theme = _region_brightness(img, 0, min(margin * 3, h))
    bot_theme = _region_brightness(img, max(0, h - gradient_height), h)
    top_color = (30, 30, 30, 255) if top_theme == "light" else (255, 255, 255, 255)
    bot_color = (30, 30, 30, 255) if bot_theme == "light" else (255, 255, 255, 255)
    bot_color_dim = (bot_color[0], bot_color[1], bot_color[2], int(255 * 0.7))

    draw = ImageDraw.Draw(img)

    font_light_italic = _load_font(FONT_SF_LIGHT_ITALIC, font_size_top)
    font_semibold = _load_font(FONT_SF_SEMIBOLD, font_size_top)
    font_joystix = _load_font(FONT_JOYSTIX, font_size_title)
    font_prompt = _load_font(FONT_SF_REGULAR, font_size_prompt)

    text_area_w = w - margin * 2

    # --- Top center: 2-line handle block ---
    # Line 1: "копируй в TG"  (LightItalic)
    # Line 2: "@roman_s_neuro" (SemiBold)
    line1 = "копируй в TG"
    line2 = "@roman_s_neuro"
    bb1 = draw.textbbox((0, 0), line1, font=font_light_italic)
    bb2 = draw.textbbox((0, 0), line2, font=font_semibold)
    line1_h = bb1[3] - bb1[1]
    line2_h = bb2[3] - bb2[1]
    block_h = line1_h + top_line_gap + line2_h

    # Align visual top of block with margin
    block_top = margin - bb1[1]
    line1_x = (w - (bb1[2] - bb1[0])) // 2
    draw.text((line1_x, block_top), line1, font=font_light_italic, fill=top_color)

    line2_x = (w - (bb2[2] - bb2[0])) // 2
    line2_y = block_top + line1_h + top_line_gap - bb2[1]
    draw.text((line2_x, line2_y), line2, font=font_semibold, fill=top_color)

    # --- Bottom gradient ---
    gradient = _make_gradient(w, h, gradient_height)
    img = Image.alpha_composite(img, gradient)
    draw = ImageDraw.Draw(img)

    # --- Bottom block: prompt text + /PROMPT label ---
    # Layout (bottom → top): margin → prompt text → gap → /PROMPT label (centered)
    prompt_lines = _wrap_text(prompt, font_prompt, text_area_w, draw)
    prompt_line_h = _line_height(font_prompt, draw)
    prompt_block_h = prompt_line_h * len(prompt_lines)

    label_bbox = draw.textbbox((0, 0), "/PROMPT", font=font_joystix)
    label_visual_h = label_bbox[3] - label_bbox[1]
    label_w = label_bbox[2] - label_bbox[0]

    prompt_top = h - margin - prompt_block_h
    label_visual_bottom = prompt_top - prompt_gap
    label_visual_top = label_visual_bottom - label_visual_h
    label_y = label_visual_top - label_bbox[1]
    label_x = (w - label_w) // 2

    draw.text((label_x, label_y), "/PROMPT", font=font_joystix, fill=bot_color)

    # Draw prompt body (justified, 70% opacity)
    _draw_justified(
        draw,
        prompt_lines,
        font_prompt,
        x=margin,
        y=prompt_top,
        max_width=text_area_w,
        line_h=prompt_line_h,
        fill=bot_color_dim,
    )

    result = img.convert("RGB")
    buf = io.BytesIO()
    result.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
