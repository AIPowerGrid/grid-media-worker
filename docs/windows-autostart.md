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
(the install records your venv's `pythonw.exe` — the windowless Python — so no
console window appears; the bridge's status lives on its dashboard and its log
in `bridge-service.log` in this folder), and waits for ComfyUI if it isn't up
yet. Check it at
http://127.0.0.1:7860 — while ComfyUI is still starting, the dashboard says so.

- Status:  `.venv\Scripts\python.exe -m bridge.cli --service-status`
- Remove:  `.venv\Scripts\python.exe -m bridge.cli --uninstall-service`

What it actually does: writes one registry value under
`HKCU\...\CurrentVersion\Run` — the same mechanism apps use for "start with
Windows". It runs as your user at sign-in. It does not restart on a crash and
does not run before you sign in; for crash restarts, see the Task Scheduler
alternative at the bottom.

## 2. ComfyUI: pick your flavor

The bridge waits for ComfyUI, but it cannot start it for you — ComfyUI needs
its own auto-start:

**ComfyUI Desktop:** Settings → check "Launch at startup" (that's all).

**ComfyUI portable:** create one Scheduled Task for your user (run in
PowerShell, no admin rights needed; adjust `$dir` to your install):

```powershell
$dir = "C:\ComfyUI_windows_portable"
Register-ScheduledTask -TaskName "ComfyUI" -Force `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME") `
  -Action (New-ScheduledTaskAction -Execute "$dir\run_nvidia_gpu.bat" -WorkingDirectory $dir) `
  -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries)
```

The working directory matters: the portable `.bat` uses paths relative to its
folder. `ExecutionTimeLimit` zero lifts Task Scheduler's default 72-hour limit,
which would otherwise stop ComfyUI after three days. (`schtasks /Create /SC
ONLOGON` needs an administrator prompt — it registers an any-user trigger.)

Remove later with `Unregister-ScheduledTask -TaskName "ComfyUI" -Confirm:$false`.

## Order doesn't matter

At sign-in both start in parallel. The bridge polls ComfyUI and begins
advertising models the moment the model list is loaded; until then the
dashboard and `/api/status` show a "Waiting for ComfyUI…" message instead of an
error. If ComfyUI takes two minutes to warm up, the worker joins the grid two
minutes after login — no interaction needed.

## Task Scheduler alternative for the bridge (optional)

The Run-key install is the simple path. If you want the bridge to also come
back after a crash, use a Scheduled Task instead (and `--uninstall-service`
first so they don't double-start). Run in PowerShell from your
grid-media-worker folder, no admin rights needed:

```powershell
$py = (Resolve-Path ".venv\Scripts\pythonw.exe").Path
$me = "$env:USERDOMAIN\$env:USERNAME"
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "GridMediaBridge" -Force `
  -Trigger @((New-ScheduledTaskTrigger -AtLogOn -User $me), $watchdog) `
  -Action (New-ScheduledTaskAction -Execute $py -Argument "-m bridge.cli" -WorkingDirectory (Get-Location).Path) `
  -Settings (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries)
```

How the restart works: the second trigger fires every 5 minutes while you are
signed in. If the bridge is still running, `IgnoreNew` skips that run; if it
has exited, the bridge starts again — so a crash costs at most 5 minutes. (Task
Scheduler's own "If the task fails, restart" setting does not help here: it
covers a task that fails to launch, not a program that exits with an error.)

Remove later with `Unregister-ScheduledTask -TaskName "GridMediaBridge" -Confirm:$false`.

## Verify the whole thing once

1. `--install-service`, set up ComfyUI's autostart.
2. Sign out, sign back in. Open http://127.0.0.1:7860 — bridge up, waiting or
   advertising. No console windows anywhere.
3. When ComfyUI finishes loading: models advertised, worker registered.
4. Done — your rig now earns after every reboot without you touching anything.
