# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import io

import pytest
from PIL import Image

from bridge.image_output import encode_image_output


@pytest.mark.parametrize(
    "target,expected",
    [("image/png", "PNG"), ("image/webp", "WEBP"), ("image/jpeg", "JPEG")],
)
def test_output_matches_upload_type(target, expected):
    raw = io.BytesIO()
    Image.new("RGBA", (4, 4), (255, 0, 0, 128)).save(raw, "PNG")
    result = encode_image_output(raw.getvalue(), target)
    with Image.open(io.BytesIO(result)) as im:
        assert im.format == expected
        assert im.size == (4, 4)
    if target == "image/png":
        assert result == raw.getvalue()


@pytest.mark.parametrize(
    "data,kind", [(b"not an image", "image/webp"), (b"bad", "image/gif")]
)
def test_invalid_output_fails_instead_of_relabelling(data, kind):
    with pytest.raises((ValueError, OSError)):
        encode_image_output(data, kind)
