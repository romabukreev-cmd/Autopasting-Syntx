import io
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

FONTS_DIR = Path(__file__).parent.parent / "fonts"
DESIGN_WIDTH = 848  # all px values are relative to this

FONT_SF_LIGHT_ITALIC = "San Francisco Pro Display/SF-Pro-Display-LightItalic.otf"
FONT_SF_SEMIBOLD      = "San Francisco Pro Display/SF-Pro-Display-Semibold.otf"
FONT_SF_REGULAR       = "San Francisco Pro Display/SF-Pro-Display-Regular.otf"
FONT_JOYSTIX          = "Joystix/joystix monospace.ttf"

# Design constants at DESIGN_WIDTH=848
MARGIN          = 25   # safe zone all edges (px)
TOP_LINE_GAP    = 4    # gap between line1 and line2 of handle block
PROMPT_GAP      = 15   # gap between /PROMPT label and prompt body
FONT_SIZE_HANDLE = 20  # handle block ("копируй в TG / @roman_s_neuro")
FONT_SIZE_BODY   = 15  # prompt body text
FONT_SIZE_TITLE_NEURO = 60   # /PROMPT JoyStix for neurophoto
FONT_SIZE_TITLE_LOGO  = 70   # PROMPT JoyStix for logo
FONT_SIZE_3D_HEADER   = 80   # "Nano Banana" / "prompt" SF for 3d


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
    """Returns 'light' if avg brightness > 160, else 'dark'."""
    w = img.width
    crop = img.crop((0, y_start, w, y_end)).convert("L")
    pixels = list(crop.getdata())
    avg = sum(pixels) / len(pixels) if pixels else 0
    return "light" if avg > 160 else "dark"


def _text_color(img: Image.Image, y_start: int, y_end: int) -> tuple:
    """Dark text on light bg, white text on dark bg."""
    theme = _region_brightness(img, y_start, y_end)
    return (30, 30, 30, 255) if theme == "light" else (255, 255, 255, 255)


def _text_h(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont) -> int:
    bb = draw.textbbox((0, 0), "Ag", font=font)
    return bb[3] - bb[1]


def _text_w(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    y: int,
    img_w: int,
    fill: tuple,
    tracking_ratio: float = 0.0,
) -> None:
    """Draw text centered horizontally. tracking_ratio: -0.03 = -3% letter spacing."""
    if tracking_ratio == 0.0:
        bb = draw.textbbox((0, 0), text, font=font)
        x = (img_w - (bb[2] - bb[0])) // 2
        draw.text((x - bb[0], y), text, font=font, fill=fill)
        return

    # Character-by-character for letter spacing
    char_data = []
    for ch in text:
        bb = draw.textbbox((0, 0), ch, font=font)
        char_data.append((ch, bb, bb[2] - bb[0]))

    tracking_px = tracking_ratio * font.size
    total_w = sum(cw for _, _, cw in char_data) + tracking_px * (len(char_data) - 1)

    x = (img_w - total_w) / 2
    for i, (ch, bb, cw) in enumerate(char_data):
        draw.text((int(x - bb[0]), y), ch, font=font, fill=fill)
        x += cw + tracking_px


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
        bb = draw.textbbox((0, 0), candidate, font=font)
        if bb[2] > max_width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


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
    for i, line in enumerate(lines):
        is_last = i == len(lines) - 1
        words = line.split()
        if is_last or len(words) <= 1:
            draw.text((x, y), line, font=font, fill=fill)
        else:
            word_widths = [_text_w(draw, w, font) for w in words]
            total_word_w = sum(word_widths)
            extra = (max_width - total_word_w) / (len(words) - 1)
            cx = float(x)
            for j, (word, ww) in enumerate(zip(words, word_widths)):
                draw.text((int(cx), y), word, font=font, fill=fill)
                cx += ww + extra
        y += line_h


# ── Layout: Нейрофото ────────────────────────────────────────────────────────

def _draw_neurophoto(img: Image.Image, draw: ImageDraw.ImageDraw, prompt: str, scale: float) -> None:
    """
    Top:    копируй в TG (LightItalic) / @roman_s_neuro (SemiBold) — centered
    Bottom: /PROMPT (JoyStix 60px, centered) + prompt body (justified)
    """
    w, h = img.size
    margin       = round(MARGIN * scale)
    top_gap      = round(TOP_LINE_GAP * scale)
    prompt_gap   = round(PROMPT_GAP * scale)
    sz_handle    = round(FONT_SIZE_HANDLE * scale)
    sz_title     = round(FONT_SIZE_TITLE_NEURO * scale)
    sz_body      = round(FONT_SIZE_BODY * scale)
    text_area_w  = w - margin * 2

    f_li  = _load_font(FONT_SF_LIGHT_ITALIC, sz_handle)
    f_sb  = _load_font(FONT_SF_SEMIBOLD, sz_handle)
    f_joy = _load_font(FONT_JOYSTIX, sz_title)
    f_reg = _load_font(FONT_SF_REGULAR, sz_body)

    # Auto colors
    top_color = _text_color(img, 0, min(margin * 4, h))
    bot_color = _text_color(img, max(0, h - round(200 * scale)), h)
    bot_color_dim = (bot_color[0], bot_color[1], bot_color[2], int(255 * 0.7))

    # --- Top handle block ---
    h1 = _text_h(draw, f_li)
    h2 = _text_h(draw, f_sb)
    block_top = margin
    _draw_centered(draw, "копируй в TG", f_li, block_top, w, top_color)
    _draw_centered(draw, "@roman_s_neuro", f_sb, block_top + h1 + top_gap, w, top_color)

    # --- Bottom: /PROMPT + prompt body ---
    prompt_lines = _wrap_text(prompt, f_reg, text_area_w, draw)
    body_line_h  = _text_h(draw, f_reg)
    body_h       = body_line_h * len(prompt_lines)
    body_top     = h - margin - body_h

    lbl_bb   = draw.textbbox((0, 0), "/PROMPT", font=f_joy)
    lbl_vis_h = lbl_bb[3] - lbl_bb[1]
    lbl_y    = body_top - prompt_gap - lbl_vis_h - lbl_bb[1]
    lbl_x    = (w - (lbl_bb[2] - lbl_bb[0])) // 2

    draw.text((lbl_x, lbl_y), "/PROMPT", font=f_joy, fill=bot_color)
    _draw_justified(draw, prompt_lines, f_reg, margin, body_top, text_area_w, body_line_h, bot_color_dim)


# ── Layout: Логотипы ─────────────────────────────────────────────────────────

def _draw_logo(img: Image.Image, draw: ImageDraw.ImageDraw, prompt: str, scale: float) -> None:
    """
    Top:    PROMPT (JoyStix 70px, centered, no slash)
            копируй в TG (LightItalic) / @roman_s_neuro (SemiBold) — centered
    Bottom: prompt body (justified)
    """
    w, h = img.size
    margin      = round(MARGIN * scale)
    top_gap     = round(TOP_LINE_GAP * scale)
    sz_handle   = round(FONT_SIZE_HANDLE * scale)
    sz_title    = round(FONT_SIZE_TITLE_LOGO * scale)
    sz_body     = round(FONT_SIZE_BODY * scale)
    text_area_w = w - margin * 2
    header_gap  = round(12 * scale)   # gap between PROMPT and handle block

    f_li  = _load_font(FONT_SF_LIGHT_ITALIC, sz_handle)
    f_sb  = _load_font(FONT_SF_SEMIBOLD, sz_handle)
    f_joy = _load_font(FONT_JOYSTIX, sz_title)
    f_reg = _load_font(FONT_SF_REGULAR, sz_body)

    top_color = _text_color(img, 0, min(margin * 6, h))
    bot_color = _text_color(img, max(0, h - round(120 * scale)), h)
    bot_color_dim = (bot_color[0], bot_color[1], bot_color[2], int(255 * 0.7))

    # --- Top: PROMPT label ---
    lbl_bb = draw.textbbox((0, 0), "PROMPT", font=f_joy)
    lbl_vis_h = lbl_bb[3] - lbl_bb[1]
    lbl_y = margin - lbl_bb[1]
    lbl_x = (w - (lbl_bb[2] - lbl_bb[0])) // 2
    draw.text((lbl_x, lbl_y), "PROMPT", font=f_joy, fill=top_color)

    lbl_bottom = margin + lbl_vis_h

    # --- Handle block below PROMPT ---
    h1 = _text_h(draw, f_li)
    h2 = _text_h(draw, f_sb)
    handle_top = lbl_bottom + header_gap
    _draw_centered(draw, "копируй в TG", f_li, handle_top, w, top_color)
    _draw_centered(draw, "@roman_s_neuro", f_sb, handle_top + h1 + top_gap, w, top_color)

    # --- Bottom: prompt body ---
    prompt_lines = _wrap_text(prompt, f_reg, text_area_w, draw)
    body_line_h  = _text_h(draw, f_reg)
    body_h       = body_line_h * len(prompt_lines)
    body_top     = h - margin - body_h

    _draw_justified(draw, prompt_lines, f_reg, margin, body_top, text_area_w, body_line_h, bot_color_dim)


# ── Layout: 3D Текст ─────────────────────────────────────────────────────────

def _draw_3d(img: Image.Image, draw: ImageDraw.ImageDraw, prompt: str, scale: float) -> None:
    """
    Top:    "Nano Banana" (SF Regular 80px, centered, -3% tracking)
            "prompt"      (SF LightItalic 80px, centered, -3% tracking, 90% line spacing)
    Bottom: копируй в TG (LightItalic 20px) / @roman_s_neuro (SemiBold 20px)
            prompt body (justified 15px)
    """
    w, h = img.size
    margin      = round(MARGIN * scale)
    top_gap     = round(TOP_LINE_GAP * scale)
    sz_handle   = round(FONT_SIZE_HANDLE * scale)
    sz_header   = round(FONT_SIZE_3D_HEADER * scale)
    sz_body     = round(FONT_SIZE_BODY * scale)
    text_area_w = w - margin * 2
    handle_gap  = round(10 * scale)   # gap between handle block and prompt body

    f_li    = _load_font(FONT_SF_LIGHT_ITALIC, sz_handle)
    f_sb    = _load_font(FONT_SF_SEMIBOLD, sz_handle)
    f_reg80 = _load_font(FONT_SF_REGULAR, sz_header)
    f_li80  = _load_font(FONT_SF_LIGHT_ITALIC, sz_header)
    f_reg   = _load_font(FONT_SF_REGULAR, sz_body)

    top_color = _text_color(img, 0, min(margin * 8, h))
    bot_color = _text_color(img, max(0, h - round(150 * scale)), h)
    bot_color_dim = (bot_color[0], bot_color[1], bot_color[2], int(255 * 0.7))

    # --- Top header: "Nano Banana" + "prompt" ---
    h_nb  = _text_h(draw, f_reg80)
    h_prm = _text_h(draw, f_li80)
    # 90% line spacing: gap = -(10% of line height) — lines are slightly tighter
    line_gap_3d = round(h_nb * (-0.10))  # negative = tighter

    nb_top  = margin
    prm_top = nb_top + h_nb + line_gap_3d

    _draw_centered(draw, "Nano Banana", f_reg80, nb_top,  w, top_color, tracking_ratio=-0.03)
    _draw_centered(draw, "prompt",      f_li80,  prm_top, w, top_color, tracking_ratio=-0.03)

    # --- Bottom: handle block + prompt body ---
    h1 = _text_h(draw, f_li)
    h2 = _text_h(draw, f_sb)
    prompt_lines = _wrap_text(prompt, f_reg, text_area_w, draw)
    body_line_h  = _text_h(draw, f_reg)
    body_h       = body_line_h * len(prompt_lines)

    # Layout bottom → top: margin | body | gap | @roman | handle_gap | копируй
    body_top   = h - margin - body_h
    handle_bot = body_top - handle_gap
    line2_top  = handle_bot - h2
    line1_top  = line2_top - top_gap - h1

    _draw_centered(draw, "копируй в TG",   f_li, line1_top, w, bot_color)
    _draw_centered(draw, "@roman_s_neuro", f_sb, line2_top, w, bot_color)
    _draw_justified(draw, prompt_lines, f_reg, margin, body_top, text_area_w, body_line_h, bot_color_dim)


# ── Public API ───────────────────────────────────────────────────────────────

def apply_overlay(image_data: bytes, prompt: str, overlay_type: str) -> bytes:
    """
    Apply design overlay.
    overlay_type: "neurophoto" | "logo" | "3d"
    """
    img  = Image.open(io.BytesIO(image_data)).convert("RGBA")
    draw = ImageDraw.Draw(img)
    scale = img.width / DESIGN_WIDTH

    if overlay_type == "logo":
        _draw_logo(img, draw, prompt, scale)
    elif overlay_type == "3d":
        _draw_3d(img, draw, prompt, scale)
    else:
        _draw_neurophoto(img, draw, prompt, scale)

    result = img.convert("RGB")
    buf = io.BytesIO()
    result.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
