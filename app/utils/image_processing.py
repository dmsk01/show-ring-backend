"""
Обработка изображений для вариантов файлов (этап 8, file_handler).

Чистые функции на Pillow: ресайз с сохранением пропорций + опциональный
водяной знак. Без БД/MinIO — байты на входе, байты на выходе. Тестируется
на сгенерированной картинке.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont, ImageOps

# (kind, max_side_px, watermark) — что генерируем для каждого изображения.
VARIANTS: list[tuple[str, int, bool]] = [
    ("thumb", 200, False),
    ("medium", 1000, True),
]

WATERMARK_TEXT = "ShowTail"
JPEG_QUALITY = 85


def _apply_watermark(img: Image.Image, text: str) -> Image.Image:
    """Полупрозрачный текст в правом нижнем углу. Возвращает новый RGB."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    margin = max(6, img.width // 100)
    x = img.width - tw - margin
    y = img.height - th - margin
    # Тень + светлый текст — читаемость на любом фоне.
    draw.text((x + 1, y + 1), text, font=font, fill=(0, 0, 0, 130))
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 200))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def make_variant(
    image_bytes: bytes, max_size: int, watermark: bool = False
) -> tuple[bytes, int, int]:
    """
    Вариант изображения: ресайз (не больше max_size по большей стороне,
    пропорции сохранены, без апскейла) + опц. водяной знак.
    Возвращает (jpeg_bytes, width, height).
    """
    img = Image.open(io.BytesIO(image_bytes))
    # EXIF-ориентация (фото с телефона) — нормализуем.
    img = ImageOps.exif_transpose(img)
    # JPEG не умеет альфу — RGBA/PNG кладём на белый фон.
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    # thumbnail сохраняет пропорции и НЕ увеличивает маленькие.
    img.thumbnail((max_size, max_size))
    if watermark:
        img = _apply_watermark(img, WATERMARK_TEXT)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue(), img.width, img.height
