import io
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Overlay config — adjust visually during testing
GRADIENT_HEIGHT_RATIO = 0.4   # bottom 40% of image
GRADIENT_OPACITY_MAX = 180    # 0-255
TEXT_COLOR = (255, 255, 255)
TEXT_PADDING = 40
FONT_SIZE_PROMPT = 28
FONT_SIZE_CTA = 22
CTA_TEXT = "@syntex_ai"       # TODO: уточнить у пользователя


def _make_gradient(width: int, height: int, gradient_height: int) -> Image.Image:
    gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(gradient)
    for y in range(gradient_height):
        alpha = int(GRADIENT_OPACITY_MAX * (y / gradient_height))
        draw.line(
            [(0, height - gradient_height + y), (width, height - gradient_height + y)],
            fill=(0, 0, 0, alpha),
        )
    return gradient


def _load_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] > max_width and current:
            lines.append(current)
            current = word
        else:
            current = test
    if current:
        lines.append(current)
    return lines


def apply_overlay(image_data: bytes, short_prompt: str) -> bytes:
    """Apply dark gradient + short prompt + CTA to image. Returns JPEG bytes."""
    img = Image.open(io.BytesIO(image_data)).convert("RGBA")
    w, h = img.size

    gradient_h = int(h * GRADIENT_HEIGHT_RATIO)
    gradient = _make_gradient(w, h, gradient_h)
    img = Image.alpha_composite(img, gradient)

    draw = ImageDraw.Draw(img)
    font_prompt = _load_font(FONT_SIZE_PROMPT)
    font_cta = _load_font(FONT_SIZE_CTA)

    max_text_width = w - TEXT_PADDING * 2

    # Draw CTA at bottom
    cta_bbox = draw.textbbox((0, 0), CTA_TEXT, font=font_cta)
    cta_h = cta_bbox[3] - cta_bbox[1]
    cta_y = h - TEXT_PADDING - cta_h
    draw.text((TEXT_PADDING, cta_y), CTA_TEXT, font=font_cta, fill=TEXT_COLOR)

    # Draw prompt text above CTA
    lines = _wrap_text(short_prompt, font_prompt, max_text_width, draw)
    line_bbox = draw.textbbox((0, 0), "Ag", font=font_prompt)
    line_h = line_bbox[3] - line_bbox[1] + 6
    total_text_h = line_h * len(lines)
    text_y = cta_y - total_text_h - 12

    for line in lines:
        draw.text((TEXT_PADDING, text_y), line, font=font_prompt, fill=TEXT_COLOR)
        text_y += line_h

    # Convert back to RGB JPEG
    result = img.convert("RGB")
    buf = io.BytesIO()
    result.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
