#!/usr/bin/env python3
"""Synthetic in-memory label views; never reads user images or calls OCR."""
import base64
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from PIL import Image, ImageDraw
import label_vision as vision


def encoded(image, format="PNG", **kwargs):
    output = io.BytesIO(); image.save(output, format=format, **kwargs)
    return output.getvalue()


def decoded(views):
    result = []
    for value in views:
        assert value.startswith("data:image/jpeg;base64,")
        image = Image.open(io.BytesIO(base64.b64decode(value.split(",", 1)[1], validate=True)))
        image.load()
        assert image.mode == "RGB" and image.format == "JPEG"
        assert not image.getexif() and max(image.size) <= 1600
        result.append(image)
    return result


def error_case(value, expected):
    try:
        vision.prepare_label_images(value)
    except vision.LabelVisionError as error:
        assert error.code == expected and error.message
    else:
        raise AssertionError(expected)


def main():
    label = Image.new("RGB", (800, 600), (120, 115, 100))
    draw = ImageDraw.Draw(label)
    draw.rectangle((60, 80, 740, 550), fill=(225, 225, 220))
    draw.text((70, 410), "ITEM NO: TEST4321CX", fill="black")
    views = vision.prepare_label_images(encoded(label))
    output = decoded(views)
    assert len(output) == 3 and output[0].size == (800, 600)
    for image, bounds in zip(output[1:], ((0.06, 0.10, 0.98, 0.96), (0.0, 0.32, 0.86, 1.0))):
        crop_width = round(800*bounds[2]) - int(800*bounds[0])
        crop_height = round(600*bounds[3]) - int(600*bounds[1])
        assert image.width <= crop_width*2 and image.height <= crop_height*2
    assert len(json.dumps(views).encode()) <= 8*1024*1024
    assert views == vision.prepare_label_images(encoded(label)), "preprocessing must be stable"
    # The complete view preserves orientation and strips even GPS/EXIF text.
    exif = Image.Exif(); exif[274] = 6; exif[270] = "synthetic private metadata"
    rotated = decoded(vision.prepare_label_images(encoded(Image.new("RGB", (80, 40), "white"), "JPEG", exif=exif)))
    assert rotated[0].size == (40, 80)
    transparent = decoded(vision.prepare_label_images(encoded(Image.new("RGBA", (30, 40), (0, 0, 0, 0)))))
    assert min(transparent[0].getpixel((0, 0))) >= 250
    assert len(decoded(vision.prepare_label_images(encoded(Image.new("RGB", (1, 1), "white"))))) <= 3
    error_case(b"", "image_invalid"); error_case("not bytes", "image_invalid")
    error_case(b"not an image", "image_invalid")
    error_case(b"x"*(6*1024*1024+1), "image_too_large")
    error_case(encoded(Image.new("1", (5000, 4001))), "image_pixels_exceeded")
    error_case(encoded(label)[:64], "image_invalid")
    # Bound output JSON independently of source compression characteristics.
    with patch.object(vision, "MAX_IMAGES_JSON_BYTES", 8):
        error_case(encoded(label), "image_output_too_large")
    assert "TEST4321" not in vision.LABEL_SYSTEM_PROMPT + vision.LABEL_USER_PROMPT
    assert all(token in vision.LABEL_SYSTEM_PROMPT for token in ("ITEM NO", "ART NO", "CX", "uncertain", "evidence"))
    print("label-vision contract: RGB/EXIF, overlapping crops, 2x/1600 scaling, bounded input/pixels/JSON, metadata stripping and invalid images passed")


if __name__ == "__main__":
    main()
