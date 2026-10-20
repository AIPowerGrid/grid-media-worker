# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Installed decoder/trust floors, independent of model and network execution."""

import io
import ssl
import subprocess
from importlib.metadata import PackageNotFoundError, version

import certifi
import pytest
from PIL import Image


@pytest.mark.parametrize(
    "package,floor", [("pillow", (13, 0, 0)), ("certifi", (2026, 10, 19))]
)
def test_installed_decoder_transport_security_floor(package, floor):
    assert tuple(int(part) for part in version(package).split(".")) >= floor


def test_rejected_gif_frame_does_not_poison_surviving_canvas():
    # GHSA-65qm-jvmh-6pqg: inspect only; never load the rejected huge frame.
    raw = bytes.fromhex(
        "47494638396101000100800000000000ffffff2c00000000010001000002024401002c00000000409c409c002c00000000010001000002024401003b",
    )
    with Image.open(io.BytesIO(raw), formats=["GIF"]) as image:
        assert image.size == (1, 1)
        with pytest.raises(Image.DecompressionBombError):
            image.seek(1)
        assert image.size == (1, 1)


def test_updated_ca_bundle_preserves_certificate_and_hostname_verification():
    context = ssl.create_default_context(cafile=certifi.where())
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.cert_store_stats()["x509_ca"] > 0


def test_release_tool_has_the_reviewed_version_and_runs():
    try:
        installed = version("uv")
    except PackageNotFoundError:
        pytest.skip("uv is a release extra; packaging jobs must run this check")
    import uv

    assert installed == "0.13.4"
    result = subprocess.run(
        [uv.find_uv_bin(), "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.split()[:2] == ["uv", installed]
