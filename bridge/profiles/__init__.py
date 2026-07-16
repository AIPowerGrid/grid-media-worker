# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Signed worker-profile contracts and local compatibility evaluation."""

from .hardware import (
    AcceleratorInfo,
    HardwareSnapshot,
    Recommendation,
    detect_hardware,
    evaluate_hardware,
)
from .advertisement import ProfileAdvertisement, load_profile_advertisement
from .installer import InstallError, InstalledArtifact, ProfileInstaller
from .profile import (
    ProfileDocument,
    ProfileError,
    ProfileSignatureError,
    ProfileValidationError,
    load_profile,
)

__all__ = [
    "AcceleratorInfo",
    "HardwareSnapshot",
    "InstallError",
    "InstalledArtifact",
    "ProfileDocument",
    "ProfileAdvertisement",
    "ProfileError",
    "ProfileSignatureError",
    "ProfileInstaller",
    "ProfileValidationError",
    "Recommendation",
    "detect_hardware",
    "evaluate_hardware",
    "load_profile",
    "load_profile_advertisement",
]
