"""Cross-platform auto-start installation for the ComfyUI bridge.

Ported from the text worker's proven service module, adapted to the bridge's
shape: the bridge is a venv install (never a frozen binary, so no /opt copy
step), its config and workflows resolve install-relatively (see config.py), and
its required backend — ComfyUI — may not be running when the service starts
(ws_worker waits for it with an authored status message).

Per-OS mechanisms, deliberately the same trade-offs as the text worker:

- Windows: an HKCU Run registry value. Starts at user LOGIN (not machine
  boot), runs as the user, needs no admin rights, and nothing restarts it on a
  crash. Operators who want boot-time start and crash retries get a documented
  Task Scheduler alternative (docs/windows-autostart.md).
- Linux: a system systemd unit installed through ONE sudo/pkexec shell, with
  Restart=on-failure. The unit name is distinct from the hand-written
  aipg-media-worker.service some deployments already run — never collide.
- macOS: a launchd LaunchAgent (login, KeepAlive).
"""

import os
import sys
from pathlib import Path

from .config import REPO_ROOT

_SERVICE_NAME = "grid-media-worker-bridge"
_SERVICE_DESC = "Grid Media Worker Bridge — AI Power Grid (ComfyUI)"

# Windows: HKCU Run key
_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_APP_NAME = "GridMediaWorker"
# Windows --restart-on-crash: a per-user scheduled task (no admin rights)
# that starts at sign-in plus a watchdog trigger. MultipleInstances=IgnoreNew
# skips the watchdog run while the bridge is up; once it has exited, the next
# run starts it again. Task Scheduler's own "restart on failure" setting only
# covers a task that fails to launch, not a program that exits.
_WIN_TASK_NAME = "GridMediaBridge"
_WIN_WATCHDOG_MINUTES = 5

# Linux: systemd unit
_SYSTEMD_SYSTEM_DIR = Path("/etc/systemd/system")
_SYSTEMD_UNIT = f"{_SERVICE_NAME}.service"

# macOS: launchd plist
_LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
_LAUNCHD_LABEL = "io.aipowergrid.media-worker"
_LAUNCHD_PLIST = f"{_LAUNCHD_LABEL}.plist"

# The bridge's dashboard port, released by the installing process before the
# service's delayed start grabs it.
_BRIDGE_PORT_DELAY_S = 3


def _exec_argv() -> list:
    """The service's launch command as an argv list.

    sys.executable is the venv's own python, which always knows its DLLs and
    site-packages — never a pip script wrapper, whose embedded interpreter path
    breaks when Python is moved or upgraded. The frozen branch is kept only for
    safety; the bridge does not ship frozen.
    """
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    exe = Path(sys.executable)
    if sys.platform == "win32":
        # python.exe is a console-subsystem binary: launched from the Run key
        # it opens a persistent console window at every sign-in. pythonw.exe —
        # its windowless twin, always next to it in a venv's Scripts/ — keeps
        # the login start invisible (logs live on the dashboard, not a
        # terminal). Fall back to python.exe if it is somehow absent.
        windowless = exe.with_name("pythonw.exe")
        if windowless.exists():
            exe = windowless
    return [str(exe), "-m", "bridge.cli"]


def _exec_command() -> str:
    """The launch command as one shell-ready string.

    The interpreter path is quoted on Windows always ("C:\\Users\\John Doe\\…")
    and elsewhere when it contains a space — systemd's ExecStart honors double
    quotes too. The -m args never need quoting."""
    argv = _exec_argv()
    head = argv[0]
    if sys.platform == "win32" or " " in head:
        head = f'"{head}"'
    return " ".join([head] + argv[1:])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_installed() -> bool:
    if sys.platform == "win32":
        return _win_is_installed()
    if sys.platform == "darwin":
        return (_LAUNCHD_DIR / _LAUNCHD_PLIST).exists()
    return (_SYSTEMD_SYSTEM_DIR / _SYSTEMD_UNIT).exists()


def install(verbose: bool = True, start: bool = True,
            restart_on_crash: bool = False) -> bool:
    """Install auto-start. On Windows, restart_on_crash selects the Task
    Scheduler watchdog instead of the Run value; systemd and launchd already
    restart the bridge, so it changes nothing there."""
    if sys.platform == "win32":
        if restart_on_crash:
            return _win_install_task(verbose, start)
        return _win_install(verbose)
    if restart_on_crash and verbose:
        print("  Note: this service manager already restarts the bridge if it stops.")
    if sys.platform == "darwin":
        return _macos_install(verbose, start)
    return _linux_install(verbose, start)


def uninstall(verbose: bool = True) -> bool:
    if sys.platform == "win32":
        return _win_uninstall(verbose)
    if sys.platform == "darwin":
        return _macos_uninstall(verbose)
    return _linux_uninstall(verbose)


def status() -> int:
    """Print installation status for --service-status.

    Returns the exit code: 1 when auto-start is installed on Windows but the
    bridge is not running (so scripts can detect a crash), else 0."""
    if sys.platform == "win32":
        return _win_status()
    if not is_installed():
        print("  Auto-start is not installed.")
        print("  Install with: comfy-bridge --install-service")
        return 0
    if sys.platform == "darwin":
        import subprocess
        print("  Auto-start installed (launchd).")
        result = subprocess.run(
            ["launchctl", "list", _LAUNCHD_LABEL], capture_output=True, text=True
        )
        print("  Status: running" if result.returncode == 0 else "  Status: not running")
    else:
        import subprocess
        print("  Auto-start installed (systemd).")
        subprocess.run(["systemctl", "status", _SERVICE_NAME, "--no-pager", "-l"])
    return 0


def schedule_start() -> None:
    """Windows only: start the bridge now, after a short delay.

    The Run value fires only at the NEXT sign-in; this gives the operator a
    running bridge immediately. The delay lets the process that ran
    --install-service (possibly a bridge) exit and release :7860 first. On
    Linux/macOS install() starts the service itself, so there is nothing to do.
    """
    import subprocess
    if sys.platform != "win32":
        return
    argv = _exec_argv()
    quoted = " ".join([f'"{argv[0]}"'] + argv[1:])
    subprocess.Popen(
        f"cmd /c ping -n {_BRIDGE_PORT_DELAY_S + 1} 127.0.0.1 >nul 2>&1 & {quoted}",
        creationflags=0x08000000 | 0x00000200,  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
    )


# ---------------------------------------------------------------------------
# Windows: is the bridge actually running? (--service-status)
# ---------------------------------------------------------------------------

def _bridge_url() -> str:
    from .config import Settings
    host = str(Settings.BRIDGE_HOST)
    if ":" in host:  # IPv6 loopback
        host = f"[{host}]"
    return f"http://{host}:{Settings.BRIDGE_PORT}"


def _probe_bridge(timeout: float = 2.0):
    """The running bridge's /api/status as a dict, None when nothing answers,
    or {} when something answers that is not a bridge."""
    import http.client
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{_bridge_url()}/api/status", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.HTTPError, http.client.HTTPException, ValueError):
        return {}
    except OSError:
        return None
    return data if isinstance(data, dict) and "worker_running" in data else {}


def _describe_worker(data: dict) -> str:
    advertised = data.get("advertised") or []
    if data.get("worker_error"):
        return str(data["worker_error"])
    if data.get("worker_running") and advertised:
        return f"connected to the grid, advertising {len(advertised)} model(s)"
    if data.get("worker_running"):
        return "starting"
    return "worker not running (open the dashboard for details)"


def _service_log_tail(lines: int = 10) -> list:
    """The last lines the windowless bridge wrote before it stopped."""
    for name in ("bridge-service.log", "bridge-service.log.1"):
        path = REPO_ROOT / name
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 64 * 1024))
                text = f.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        tail = [line for line in text.splitlines() if line.strip()][-lines:]
        if tail:
            return [f"({name})"] + tail
    return []


def _win_status() -> int:
    run_key = _win_run_value_installed()
    task = _win_task_installed()
    if task:
        print(f'  Auto-start installed: restart-on-crash (scheduled task "{_WIN_TASK_NAME}" —')
        print(f"  starts at sign-in and restarts the bridge within {_WIN_WATCHDOG_MINUTES} minutes if it stops).")
    if run_key:
        print("  Auto-start installed: at sign-in (Windows Run key; no restart if it stops).")
    if not (task or run_key):
        print("  Auto-start is not installed.")
        print("  Install with: comfy-bridge --install-service")

    data = _probe_bridge()
    url = _bridge_url()
    if data:
        print(f"  Bridge: running at {url} — {_describe_worker(data)}")
        return 0
    if data == {}:
        print(f"  Bridge: something else answers at {url} — it is not a Comfy Bridge.")
    else:
        print(f"  Bridge: NOT running — nothing answers at {url}")
    if not (task or run_key):
        return 0
    if task:
        print(f"  The watchdog starts it again within {_WIN_WATCHDOG_MINUTES} minutes.")
    else:
        print("  It starts again at your next sign-in, or now with: comfy-bridge --install-service")
    tail = _service_log_tail()
    if tail:
        print(f"  Last lines of {tail[0][1:-1]}:")
        for line in tail[1:]:
            print(f"    {line}")
    return 1


# ---------------------------------------------------------------------------
# Windows (HKCU Run value)
# ---------------------------------------------------------------------------

def _win_is_installed() -> bool:
    return _win_run_value_installed() or _win_task_installed()


def _win_run_value_installed() -> bool:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, _WIN_APP_NAME)
            return True
        except OSError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def _win_install(verbose: bool = True) -> bool:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, _WIN_APP_NAME, 0, winreg.REG_SZ, _exec_command())
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        if verbose:
            print(f"  Error: {e}")
        return False
    # One mode at a time: a leftover watchdog task would race this Run value
    # for the dashboard port at every sign-in.
    replaced = _win_delete_task()
    if verbose:
        print("  Auto-start installed (Windows startup).")
        if replaced == "removed":
            print(f'  (Replaced the restart-on-crash task "{_WIN_TASK_NAME}".)')
        print()
        print("  The bridge will start when you log in and wait for ComfyUI.")
        print("  It is not restarted if it stops; for that, reinstall with")
        print("  comfy-bridge --install-service --restart-on-crash")
        print("  ComfyUI needs its own auto-start — see docs/windows-autostart.md")
        print("  To remove: comfy-bridge --uninstall-service")
    return True


def _win_remove_run_value() -> str:
    """'removed', 'absent', or the error text."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, _WIN_APP_NAME)
        finally:
            winreg.CloseKey(key)
        return "removed"
    except OSError as e:
        # Missing value = not installed; anything else (e.g. a registry
        # permission problem) deserves its real error, not that claim.
        if isinstance(e, FileNotFoundError) or getattr(e, "winerror", None) == 2 or e.errno == 2:
            return "absent"
        return str(e)
    except Exception as e:
        return str(e)


def _win_uninstall(verbose: bool = True) -> bool:
    run_value = _win_remove_run_value()
    task = _win_delete_task()
    errors = [r for r in (run_value, task) if r not in ("removed", "absent")]
    removed = "removed" in (run_value, task)
    if verbose:
        if run_value == "removed":
            print("  Auto-start removed from Windows startup.")
        if task == "removed":
            print(f'  Restart-on-crash task "{_WIN_TASK_NAME}" removed.')
        for error in errors:
            print(f"  Error: {error}")
        if not removed and not errors:
            print("  Auto-start was not installed.")
        if removed:
            print("  A bridge running right now keeps running until you close it or sign out.")
    return removed and not errors


# ---------------------------------------------------------------------------
# Windows --restart-on-crash (per-user scheduled task)
# ---------------------------------------------------------------------------

def _schtasks(*args):
    """Run schtasks.exe; (returncode, combined output). Never raises."""
    import subprocess
    try:
        result = subprocess.run(
            ["schtasks", *args], capture_output=True, text=True, errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        return 1, str(e)
    return result.returncode, (result.stdout + result.stderr).strip()


def _win_task_installed() -> bool:
    return _schtasks("/Query", "/TN", _WIN_TASK_NAME)[0] == 0


def _win_delete_task() -> str:
    """'removed', 'absent', or the error text."""
    if not _win_task_installed():
        return "absent"
    code, output = _schtasks("/Delete", "/TN", _WIN_TASK_NAME, "/F")
    return "removed" if code == 0 else (output or f"schtasks exited {code}")


def _win_task_user() -> str:
    domain, name = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME")
    if domain and name:
        return f"{domain}\\{name}"
    import getpass
    return getpass.getuser()


def _win_task_xml(start_boundary: str = None) -> str:
    """Task definition for schtasks /XML. Both triggers name the current user:
    an any-user logon trigger needs an administrator, a per-user one does not."""
    import datetime
    from xml.sax.saxutils import escape

    if start_boundary is None:
        start_boundary = datetime.datetime.now().replace(microsecond=0).isoformat()
    argv = _exec_argv()
    user = escape(_win_task_user())
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(_SERVICE_DESC)}: start at sign-in; restart within {_WIN_WATCHDOG_MINUTES} minutes if it stops.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
    <TimeTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>{start_boundary}</StartBoundary>
      <Repetition>
        <Interval>PT{_WIN_WATCHDOG_MINUTES}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(argv[0])}</Command>
      <Arguments>{escape(" ".join(argv[1:]))}</Arguments>
      <WorkingDirectory>{escape(str(REPO_ROOT))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _win_install_task(verbose: bool = True, start: bool = True) -> bool:
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-16") as f:
            f.write(_win_task_xml())
        code, output = _schtasks("/Create", "/TN", _WIN_TASK_NAME, "/XML", path, "/F")
    finally:
        os.remove(path)
    if code != 0:
        if verbose:
            print(f"  Error: could not create the scheduled task: {output}")
        return False
    # One mode at a time (see _win_install).
    replaced = _win_remove_run_value()
    if verbose:
        print("  Auto-start installed with restart-on-crash")
        print(f'  (scheduled task "{_WIN_TASK_NAME}", no admin rights needed).')
        if replaced == "removed":
            print("  (Replaced the sign-in-only Run key entry.)")
        print()
        print("  The bridge starts when you sign in and waits for ComfyUI. If it")
        print(f"  stops, it is started again within {_WIN_WATCHDOG_MINUTES} minutes while you are signed in.")
        print("  ComfyUI needs its own auto-start — see docs/windows-autostart.md")
        print("  Check it: comfy-bridge --service-status")
        print("  To remove: comfy-bridge --uninstall-service")
    if start:
        if _probe_bridge():
            if verbose:
                print("  A bridge is already running; the watchdog takes over if it stops.")
        else:
            code, output = _schtasks("/Run", "/TN", _WIN_TASK_NAME)
            if verbose:
                print("  Bridge started." if code == 0 else f"  Could not start it now: {output}")
    return True


# ---------------------------------------------------------------------------
# Linux (systemd system unit)
# ---------------------------------------------------------------------------

def _systemd_unit_content() -> str:
    import getpass
    return f"""[Unit]
Description={_SERVICE_DESC}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={getpass.getuser()}
WorkingDirectory={REPO_ROOT}
ExecStart={_exec_command()}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


def _linux_install(verbose: bool = True, start: bool = True) -> bool:
    import shlex
    import tempfile

    unit_content = _systemd_unit_content()
    unit_path = _SYSTEMD_SYSTEM_DIR / _SYSTEMD_UNIT

    fd = tempfile.NamedTemporaryFile(mode="w", suffix=".service", delete=False)
    fd.write(unit_content)
    fd.close()
    tmp = Path(fd.name)

    cmds = (
        f"cp {shlex.quote(str(tmp))} {shlex.quote(str(unit_path))} && "
        f"systemctl daemon-reload && "
        f"systemctl enable {shlex.quote(_SERVICE_NAME)}"
    )
    if start:
        cmds += f" && systemctl start {_SERVICE_NAME}"
    # No delayed-start shell here: a trailing '&' would background the whole
    # &&-list, making sudo report success before cp even ran (and racing the
    # temp-file cleanup below). Callers that pass start=False get told how to
    # start the service themselves.

    if _run_privileged(cmds):
        tmp.unlink(missing_ok=True)
        if verbose:
            print("  System service installed.")
            if not start:
                print(f"  Start it with: sudo systemctl start {_SERVICE_NAME}")
            print()
            print("  Commands:")
            print(f"    sudo systemctl status {_SERVICE_NAME}")
            print(f"    sudo systemctl restart {_SERVICE_NAME}")
            print(f"    journalctl -u {_SERVICE_NAME} -f")
            print()
            print("  To remove: comfy-bridge --uninstall-service")
        return True
    tmp.unlink(missing_ok=True)
    if verbose:
        _print_manual_linux_install(unit_content)
    return False


def _run_privileged(cmds: str) -> bool:
    """Run a root shell snippet; True on success.

    Root runs directly; a terminal uses sudo (works headless over SSH); a
    desktop session without a terminal uses pkexec's graphical prompt.
    """
    import shutil
    import subprocess

    if os.geteuid() == 0:
        launcher: list = []
    elif sys.stdin is not None and sys.stdin.isatty() and shutil.which("sudo"):
        launcher = ["sudo"]
    elif shutil.which("pkexec") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        launcher = ["pkexec"]
    else:
        return False
    try:
        result = subprocess.run([*launcher, "bash", "-c", cmds], stdout=subprocess.DEVNULL)
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0


def _print_manual_linux_install(unit_content: str) -> None:
    import shlex
    unit_path = _SYSTEMD_SYSTEM_DIR / _SYSTEMD_UNIT
    print("  Could not get administrator rights (no sudo terminal or desktop prompt).")
    # Not "sudo comfy-bridge": run as root, the unit would be generated for
    # root's account and look for root's config instead of this operator's.
    print("  Install it by hand from this account:")
    print()
    print(f"    sudo tee {shlex.quote(str(unit_path))} >/dev/null <<'UNIT'")
    print(unit_content.rstrip())
    print("UNIT")
    print("    sudo systemctl daemon-reload")
    print(f"    sudo systemctl enable --now {_SERVICE_NAME}")


def _linux_uninstall(verbose: bool = True) -> bool:
    import shlex

    unit_path = _SYSTEMD_SYSTEM_DIR / _SYSTEMD_UNIT
    if not unit_path.exists():
        if verbose:
            print("  Auto-start was not installed.")
        return False
    cmds = (
        f"systemctl stop {_SERVICE_NAME}; "
        f"systemctl disable {_SERVICE_NAME}; "
        f"rm -f {shlex.quote(str(unit_path))}; "
        f"systemctl daemon-reload"
    )
    if _run_privileged(cmds):
        if verbose:
            print("  System service stopped and removed.")
        return True
    if verbose:
        print("  Could not get administrator rights. Remove it manually:")
        print(f"    sudo systemctl disable --now {_SERVICE_NAME}")
        print(f"    sudo rm -f {shlex.quote(str(unit_path))}")
        print("    sudo systemctl daemon-reload")
    return False


# ---------------------------------------------------------------------------
# macOS (launchd LaunchAgent)
# ---------------------------------------------------------------------------

def _launchd_plist_content() -> str:
    from xml.sax.saxutils import escape

    # Paths are data inside XML: '&' or '<' in a directory name must not
    # produce an unparseable plist.
    arg_entries = "\n".join(
        f"      <string>{escape(a)}</string>" for a in _exec_argv()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{arg_entries}
    </array>
    <key>WorkingDirectory</key>
    <string>{REPO_ROOT}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/{_SERVICE_NAME}.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/{_SERVICE_NAME}.err</string>
</dict>
</plist>
"""


def _macos_install(verbose: bool = True, start: bool = True) -> bool:
    import subprocess
    _LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)
    plist_path = _LAUNCHD_DIR / _LAUNCHD_PLIST
    try:
        plist_path.write_text(_launchd_plist_content())
        if start:
            # Reinstall over a loaded agent: unload first (quietly — it may
            # not be loaded) so `load` can't fail with "already loaded".
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
            subprocess.run(["launchctl", "load", str(plist_path)], check=True, capture_output=True)
        if verbose:
            print("  Auto-start installed (launchd).")
            print()
            print("  Commands:")
            print(f"    launchctl list | grep {_LAUNCHD_LABEL}")
            print(f"    tail -f /tmp/{_SERVICE_NAME}.log")
            print()
            print("  To remove: comfy-bridge --uninstall-service")
        return True
    except Exception as e:
        if verbose:
            print(f"  Error: {e}")
        return False


def _macos_uninstall(verbose: bool = True) -> bool:
    import subprocess
    plist_path = _LAUNCHD_DIR / _LAUNCHD_PLIST
    if not plist_path.exists():
        if verbose:
            print("  Auto-start was not installed.")
        return False
    try:
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink()
        if verbose:
            print("  Auto-start stopped and removed.")
        return True
    except Exception as e:
        if verbose:
            print(f"  Error: {e}")
        return False
