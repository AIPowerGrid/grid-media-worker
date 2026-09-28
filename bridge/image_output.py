# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Match image bytes to the content type signed into a Grid upload slot."""

import io

from PIL import Image


def encode_image_output(data: bytes, content_type: str) -> bytes:
    formats = {
        "image/png": ("PNG", {}),
        "image/webp": ("WEBP", {"quality": 90, "method": 6}),
        "image/jpeg": ("JPEG", {"quality": 90}),
    }
    if content_type not in formats:
        raise ValueError("Unsupported image upload content type")
    target, options = formats[content_type]
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        if im.format == target:
            return data
        if target == "JPEG" and im.mode not in {"RGB", "L"}:
            im = im.convert("RGB")
        output = io.BytesIO()
        im.save(output, format=target, **options)
        return output.getvalue()
