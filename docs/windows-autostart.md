# Start the media worker automatically on Windows

Two programs have to be running for your rig to earn: **ComfyUI** (the renderer)
and **comfy-bridge** (the grid connection). This page sets up both to start on
their own, so forgetting to launch them stops being a thing.

## 1. The bridge: one command

From your grid-media-worker folder (any terminal, no admin rights needed):

```powershell
.venv\Scripts\python.exe -m bridge.cli --install-service
```

That's it. The bridge now starts every time you sign in to Windows, invisibly
(no console window), and waits for ComfyUI if it isn't up yet. Check it at
http://127.0.0.1:7860 — while ComfyUI is still starting, the dashboard says so.

- Status:  `.venv\Scripts\python.exe -m bridge.cli --service-status`
- Remove:  `.venv\Scripts\python.exe -m bridge.cli --uninstall-service`

What it actually does: writes one registry value under
`HKCU\...\CurrentVersion\Run` — the same mechanism apps use for "start with
Windows". It runs as your user at sign-in. It does not restart on a crash and
does not run before you sign in; if you want those, see the Task Scheduler
alternative at the bottom.

## 2. ComfyUI: pick your flavor

The bridge waits for ComfyUI, but it cannot start it for you — ComfyUI needs
its own auto-start:

**ComfyUI Desktop:** Settings → check "Launch at startup" (that's all).

**ComfyUI portable:** create one Scheduled Task (run in PowerShell, adjust the
path to your install):

```powershell
schtasks /Create /TN "ComfyUI" /SC ONLOGON /TR "C:\ComfyUI_windows_portable\run_nvidia_gpu.bat" /F
```

Remove later with `schtasks /Delete /TN "ComfyUI" /F`.

## Order doesn't matter

At sign-in both start in parallel. The bridge polls ComfyUI and begins
advertising models the moment the model list is loaded; until then the
dashboard and `/api/status` show a "Waiting for ComfyUI…" message instead of an
error. If ComfyUI takes two minutes to warm up, the worker joins the grid two
minutes after login — no interaction needed.

## Task Scheduler alternative for the bridge (optional)

The Run-key install is the simple path. If you want the bridge to also restart
after a crash, use a Scheduled Task instead (and `--uninstall-service` first so
they don't double-start):

```powershell
schtasks /Create /TN "GridMediaBridge" /SC ONLOGON ^
  /TR "\"C:\path\to\grid-media-worker\.venv\Scripts\python.exe\" -m bridge.cli" /F
```

Then in Task Scheduler (taskschd.msc) open the task's Settings tab and enable
"If the task fails, restart every 1 minute".

## Verify the whole thing once

1. `--install-service`, set up ComfyUI's autostart.
2. Sign out, sign back in. Open http://127.0.0.1:7860 — bridge up, waiting or
   advertising. No console windows anywhere.
3. When ComfyUI finishes loading: models advertised, worker registered.
4. Done — your rig now earns after every reboot without you touching anything.
