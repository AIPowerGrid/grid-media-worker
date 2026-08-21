# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify a benchmark-only Grid media manager qualification payload offline."""

from __future__ import annotations

import argparse
from pathlib import Path

from bridge.release_verifier import verify_qualification_release


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_directory", type=Path)
    args = parser.parse_args()
    verify_qualification_release(args.release_directory)
    print(f"Verified manager qualification payload in {args.release_directory}")


if __name__ == "__main__":
    main()
