# deploy - native audio worker service

## Purpose

Source-controlled Linux service wiring for a signed managed audio worker.

## Ownership

- `systemd/aipg-audio-worker@.service` - per-OS-user manager service. The
  instance name is the Linux account that owns private worker state and GPU
  access.
- `README.md` - reviewed install paths, permissions, and verification steps.

## Local Contracts

- The service may run only an active signed profile. Never add
  `--allow-unsigned-draft` to production service wiring.
- API keys, worker keys, and payout-wallet delegation certificates remain
  private `0600` files under the instance user's manager state directory.
- The service launches the local ACE-Step runtime and Grid WebSocket as one
  lifecycle. Restarting it must not leave a separate model server behind.
- Do not put credentials in the unit, command line, or environment file.

## Verification

- Run `systemd-analyze verify` against the installed unit on the target Linux
  host.
- Confirm `systemctl is-active`, one audio model in Core status, and reconnect
  after a supervised service restart.

## Child DOX Index

No child guide is required.
