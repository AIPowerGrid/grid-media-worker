# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Create an unsigned, exact-hardware pilot profile for private qualification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bridge.profiles.release import prepare_pilot_profile


def main() -> None:
    parser = argparse.ArgumentParser(prog="profile-pilot")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument(
        "--hardware-class",
        choices=("minimum", "midrange", "datacenter"),
        required=True,
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = prepare_pilot_profile(
        args.profile,
        args.out,
        hardware_class=args.hardware_class,
        force=args.force,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
