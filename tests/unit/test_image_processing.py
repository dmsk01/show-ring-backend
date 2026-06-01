import io

import pytest
from PIL import Image

import app.utils.image_processing as ip
from app.utils.image_processing import VARIANTS, make_variant


def _png(w: int, h: int, mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    color = (120, 160, 200) if mode == "RGB" else (255, 0, 0, 128)
    Image.new(mode, (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_variants_config_has_thumb_and_medium():
    kinds = [k for k, _, _ in VARIANTS]
    assert "thumb" in kinds and "medium" in kinds


def test_make_variant_resizes_and_keeps_aspect():
    data, w, h = make_variant(_png(2000, 1500), 200, watermark=False)
    assert data[:2] == b"\xff\xd8"  # JPEG magic
    assert max(w, h) <= 200
    assert (w, h) == (200, 150)  # 4:3 сохранено


def test_make_variant_no_upscale():
    _, w, h = make_variant(_png(150, 100), 1000, watermark=False)
    assert (w, h) == (150, 100)  # маленькое не увеличиваем


def test_make_variant_watermark_changes_pixels():
    raw = _png(800, 600)
    plain, _, _ = make_variant(raw, 500, watermark=False)
    wm, w, h = make_variant(raw, 500, watermark=True)
    assert wm[:2] == b"\xff\xd8"
    assert max(w, h) <= 500
    assert wm != plain  # водяной знак изменил картинку


def test_make_variant_rgba_converts_to_jpeg():
    data, _, _ = make_variant(_png(300, 300, mode="RGBA"), 200, watermark=False)
    assert data[:2] == b"\xff\xd8"  # RGBA → JPEG без ошибок


def test_make_variant_rejects_decompression_bomb(monkeypatch):
    # Защита от decompression-bomb: размеры проверяются по заголовку ДО
    # декода. Понижаем лимит и подаём картинку крупнее него.
    monkeypatch.setattr(ip, "MAX_IMAGE_PIXELS", 100)  # 10x10
    with pytest.raises(ValueError):
        make_variant(_png(200, 200), 100, watermark=False)


def test_make_variant_allows_within_pixel_limit(monkeypatch):
    # Граница: 100x100 = 10000 ≤ лимит — проходит.
    monkeypatch.setattr(ip, "MAX_IMAGE_PIXELS", 10_000)
    data, _, _ = make_variant(_png(100, 100), 50, watermark=False)
    assert data[:2] == b"\xff\xd8"
