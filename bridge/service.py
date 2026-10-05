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


def install(verbose: bool = True, start: bool = True) -> bool:
    if sys.platform == "win32":
        return _win_install(verbose)
    if sys.platform == "darwin":
        return _macos_install(verbose, start)
    return _linux_install(verbose, start)


def uninstall(verbose: bool = True) -> bool:
    if sys.platform == "win32":
        return _win_uninstall(verbose)
    if sys.platform == "darwin":
        return _macos_uninstall(verbose)
    return _linux_uninstall(verbose)


def status() -> None:
    """Print installation status for --service-status."""
    if not is_installed():
        print("  Auto-start is not installed.")
        print("  Install with: comfy-bridge --install-service")
        return
    if sys.platform == "win32":
        print("  Auto-start installed (Windows Run key — starts at login).")
        print("  Note: ComfyUI must auto-start too; see docs/windows-autostart.md")
    elif sys.platform == "darwin":
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
# Windows (HKCU Run value)
# ---------------------------------------------------------------------------

def _win_is_installed() -> bool:
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
        if verbose:
            print("  Auto-start installed (Windows startup).")
            print()
            print("  The bridge will start when you log in and wait for ComfyUI.")
            print("  ComfyUI needs its own auto-start — see docs/windows-autostart.md")
            print("  To remove: comfy-bridge --uninstall-service")
        return True
    except Exception as e:
        if verbose:
            print(f"  Error: {e}")
        return False


def _win_uninstall(verbose: bool = True) -> bool:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, _WIN_APP_NAME)
        finally:
            winreg.CloseKey(key)
        if verbose:
            print("  Auto-start removed from Windows startup.")
        return True
    except OSError as e:
        # Missing value = not installed; anything else (e.g. a registry
        # permission problem) deserves its real error, not that claim.
        if isinstance(e, FileNotFoundError) or getattr(e, "winerror", None) == 2 or e.errno == 2:
            if verbose:
                print("  Auto-start was not installed.")
        elif verbose:
            print(f"  Error: {e}")
        return False
    except Exception as e:
        if verbose:
            print(f"  Error: {e}")
        return False


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
