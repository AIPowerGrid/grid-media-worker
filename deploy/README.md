# Managed audio worker service

The systemd template runs one signed worker manager as an existing Linux user.
Install the reviewed manager binary at `/usr/local/bin/grid-media-manager` and
the signed profile at `/etc/aipg/audio-worker.profile.json`. The instance user's
private state lives at `$HOME/.aipg/media-worker`:

```text
install/
state/active-worker-state.json
state/audio-worker-key.json
state/audio-worker-delegation.json
state/audio-worker-credentials.json
```

All state files containing credentials or identity material must be mode
`0600`. The state directory and install tree must be owned by the service user.

Install and start for user `aipg-worker`:

```bash
sudo install -o root -g root -m 0755 grid-media-manager /usr/local/bin/grid-media-manager
sudo install -o root -g root -m 0644 audio-worker.profile.json /etc/aipg/audio-worker.profile.json
sudo install -o root -g root -m 0644 \
  deploy/systemd/aipg-audio-worker@.service \
  /etc/systemd/system/aipg-audio-worker@.service
sudo systemd-analyze verify /etc/systemd/system/aipg-audio-worker@.service
sudo systemctl daemon-reload
sudo systemctl enable --now aipg-audio-worker@aipg-worker.service
```

The service never contains a Grid key. The manager reads the locally enrolled
credential and delegation, verifies the signed profile and canary state, starts
the loopback ACE-Step API, and then connects to Core. Its filesystem is
read-only except for the instance user's `$HOME/.aipg/media-worker` tree.
