import io
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

FONTS_DIR = Path(__file__).parent.parent / "fonts"

# Font paths relative to fonts/
FONT_SERPANTIN = "Serpantin/Serpantin.ttf"
FONT_SF_REGULAR = "San Francisco Pro Display/SF-Pro-Display-Regular.otf"
FONT_SF_BOLD = "San Francisco Pro Display/SF-Pro-Display-Bold.otf"


@dataclass
class OverlayConfig:
    model_label: str       # top-left label, can contain \n
    tg_handle: str         # top-right, e.g. "TG: @roman_s_neuro"
    design_width: int      # Figma canvas width — all px values are relative to this
    margin: int            # safe zone on all edges (px)
    gradient_height: int   # height of bottom gradient rectangle (px)
    font_size_title: int   # Serpantin: model name + /промпт label
    font_size_prompt: int  # SF Pro Regular: prompt body text
    font_size_tg: int      # SF Pro Bold: TG handle
    prompt_gap: int        # gap between /промпт label and prompt text (px)


SEEDREAM_CONFIG = OverlayConfig(
    model_label="SEEDREAM",
    tg_handle="TG: @roman_s_neuro",
    design_width=1664,
    margin=50,
    gradient_height=650,
    font_size_title=150,
    font_size_prompt=30,
    font_size_tg=40,
    prompt_gap=30,
)

NANOBANA_CONFIG = OverlayConfig(
    model_label="NANO\nBANANA",
    tg_handle="TG: @roman_s_neuro",
    design_width=848,
    margin=25,
    gradient_height=270,
    font_size_title=75,
    font_size_prompt=15,
    font_size_tg=20,
    prompt_gap=15,
)

_CONFIGS = {
    "seedream": SEEDREAM_CONFIG,
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

    model_type: "seedream" or "nanobana"
    Returns JPEG bytes.
    """
    cfg = _CONFIGS.get(model_type)
    if cfg is None:
        raise ValueError(f"Unknown model_type: {model_type!r}. Use 'seedream' or 'nanobana'.")

    img = Image.open(io.BytesIO(image_data)).convert("RGBA")
    w, h = img.size

    # Scale all Figma pixel values proportionally to actual image size
    scale = w / cfg.design_width
    margin = round(cfg.margin * scale)
    gradient_height = round(cfg.gradient_height * scale)
    font_size_title = round(cfg.font_size_title * scale)
    font_size_prompt = round(cfg.font_size_prompt * scale)
    font_size_tg = round(cfg.font_size_tg * scale)
    prompt_gap = round(cfg.prompt_gap * scale)

    # --- Gradient ---
    gradient = _make_gradient(w, h, gradient_height)
    img = Image.alpha_composite(img, gradient)

    draw = ImageDraw.Draw(img)

    font_title = _load_font(FONT_SERPANTIN, font_size_title)
    font_prompt = _load_font(FONT_SF_REGULAR, font_size_prompt)
    font_tg = _load_font(FONT_SF_BOLD, font_size_tg)

    text_area_w = w - margin * 2

    # --- Top-left: model name (Serpantin, may be multiline) ---
    # Align visual top of title with visual top of TG handle at y=margin
    first_line = cfg.model_label.split("\n")[0]
    title_top_offset = draw.textbbox((0, 0), first_line, font=font_title)[1]
    title_y = margin - title_top_offset
    draw.multiline_text(
        (margin, title_y),
        cfg.model_label,
        font=font_title,
        fill=(255, 255, 255, 255),
        align="left",
        spacing=0,
    )

    # --- Top-right: TG handle (SF Pro Bold, right-aligned) ---
    tg_bbox = draw.textbbox((0, 0), cfg.tg_handle, font=font_tg)
    tg_w = tg_bbox[2] - tg_bbox[0]
    tg_y = margin - tg_bbox[1]
    draw.text((w - margin - tg_w, tg_y), cfg.tg_handle, font=font_tg, fill=(255, 255, 255, 255))

    # --- Bottom block: prompt text + /промпт label ---
    # Layout (bottom → top):
    #   margin → prompt text → gap → /промпт label (centered)
    prompt_lines = _wrap_text(prompt, font_prompt, text_area_w, draw)
    prompt_line_h = _line_height(font_prompt, draw)
    prompt_block_h = prompt_line_h * len(prompt_lines)

    # Use actual bbox of "/промпт" for accurate height
    label_bbox = draw.textbbox((0, 0), "/промпт", font=font_title)
    label_visual_h = label_bbox[3] - label_bbox[1]
    label_w = label_bbox[2] - label_bbox[0]

    # Prompt text starts at prompt_top (drawing y)
    prompt_top = h - margin - prompt_block_h
    # Visual bottom of /промпт = visual top of prompt text - gap
    label_visual_bottom = prompt_top - prompt_gap
    label_visual_top = label_visual_bottom - label_visual_h
    # Adjust drawing y for bbox top offset
    label_y = label_visual_top - label_bbox[1]
    # Center /промпт horizontally
    label_x = (w - label_w) // 2

    draw.text(
        (label_x, label_y),
        "/промпт",
        font=font_title,
        fill=(255, 255, 255, 255),
    )

    # Draw prompt body (justified, 70% opacity)
    _draw_justified(
        draw,
        prompt_lines,
        font_prompt,
        x=margin,
        y=prompt_top,
        max_width=text_area_w,
        line_h=prompt_line_h,
        fill=(255, 255, 255, int(255 * 0.7)),
    )

    result = img.convert("RGB")
    buf = io.BytesIO()
    result.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
