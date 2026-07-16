#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline CLI for final Worker Profile V1 signing."""

from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path

from bridge.profiles.release import finalize_profile


def _qualification(value: str) -> tuple[str, Path]:
    hardware_class, separator, path = value.partition("=")
    if separator != "=" or hardware_class not in {"minimum", "midrange", "datacenter"}:
        raise argparse.ArgumentTypeError(
            "qualification must be minimum=PATH, midrange=PATH, or datacenter=PATH"
        )
    return hardware_class, Path(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="profile-release")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--recipe-vault-root", required=True)
    parser.add_argument(
        "--qualification",
        action="append",
        type=_qualification,
        required=True,
        metavar="CLASS=PATH",
        help="private benchmark report; provide minimum, midrange, and datacenter",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--ask-pass",
        action="store_true",
        help="prompt for an encrypted Ed25519 PEM password",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    qualification_reports = dict(args.qualification)
    if len(qualification_reports) != len(args.qualification):
        _parser().error("each qualification class may be provided only once")
    result = finalize_profile(
        args.profile,
        args.out,
        args.private_key,
        key_id=args.key_id,
        recipe_vault_root=args.recipe_vault_root,
        qualification_reports=qualification_reports,
        force=args.force,
        private_key_password=(
            getpass.getpass("Release key password: ").encode("utf-8")
            if args.ask_pass
            else None
        ),
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
