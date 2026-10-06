"""Auto-start installation (--install-service) across the three platforms.

The Windows registry logic is exercised everywhere by injecting a fake winreg
module (the code touches the registry through four calls only); the systemd
unit and launchd plist are validated as content. What no test here can prove —
that Windows actually launches the Run value at login — is covered by the
manual acceptance script in docs/windows-autostart.md.
"""
import sys
import types

import pytest

from bridge import service
from bridge.config import REPO_ROOT


class FakeWinreg(types.ModuleType):
    """Minimal HKCU Run-key stand-in: one dict, same four calls."""

    HKEY_CURRENT_USER = object()
    KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 1

    def __init__(self):
        super().__init__("winreg")
        self.values = {}

    def OpenKey(self, root, path, reserved, access):
        assert path == service._WIN_RUN_KEY
        return "key-handle"

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise OSError(2, "not found")
        return self.values[name], self.REG_SZ

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise OSError(2, "not found")
        del self.values[name]

    def CloseKey(self, key):
        pass


class FakeSchtasks:
    """schtasks.exe stand-in: task name -> registered XML."""

    def __init__(self):
        self.tasks = {}
        self.runs = []

    def __call__(self, *args):
        op, name = args[0], args[2]
        if op == "/Query":
            return (0, "") if name in self.tasks else (1, "ERROR: cannot find")
        if op == "/Create":
            with open(args[4], encoding="utf-16") as f:
                self.tasks[name] = f.read()
            return 0, "SUCCESS"
        if op == "/Delete":
            return (0, "SUCCESS") if self.tasks.pop(name, None) else (1, "ERROR")
        if op == "/Run":
            self.runs.append(name)
            return 0, "SUCCESS"
        raise AssertionError(f"unexpected schtasks call {args}")


@pytest.fixture
def win(monkeypatch):
    fake = FakeWinreg()
    fake.schtasks = FakeSchtasks()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(service.sys, "platform", "win32")
    # Never touch the real Task Scheduler or a real bridge port from tests.
    monkeypatch.setattr(service, "_schtasks", fake.schtasks)
    monkeypatch.setattr(service, "_probe_bridge", lambda timeout=2.0: None)
    return fake


def test_windows_install_writes_quoted_run_value(win, monkeypatch, capsys):
    monkeypatch.setattr(
        service.sys, "executable", r"C:\Users\John Doe\venv\Scripts\python.exe"
    )
    assert service.install() is True
    cmd = win.values[service._WIN_APP_NAME]
    # Path with spaces must be quoted; module launch must survive any cwd.
    assert cmd == '"C:\\Users\\John Doe\\venv\\Scripts\\python.exe" -m bridge.cli'
    assert service.is_installed() is True
    out = capsys.readouterr().out
    assert "ComfyUI needs its own auto-start" in out


def test_windows_uninstall_removes_value_and_is_idempotent(win):
    service.install(verbose=False)
    assert service.uninstall(verbose=False) is True
    assert service.is_installed() is False
    # Second uninstall: not installed → False, no exception.
    assert service.uninstall(verbose=False) is False


def test_systemd_unit_content_is_service_ready(monkeypatch):
    import getpass

    monkeypatch.setattr(service.sys, "executable", "/opt/venv/bin/python")
    unit = service._systemd_unit_content()
    assert "ExecStart=/opt/venv/bin/python -m bridge.cli" in unit
    assert f"WorkingDirectory={REPO_ROOT}" in unit
    assert "Restart=on-failure" in unit
    assert "After=network-online.target" in unit
    # Runs as the installing operator (whose .env it is), never hardcoded root.
    assert f"User={getpass.getuser()}" in unit


def test_launchd_plist_keeps_argv_separate(monkeypatch):
    # The text worker split a quoted command string here, which breaks on
    # paths with spaces; the port must emit one <string> per argv element.
    monkeypatch.setattr(service.sys, "frozen", False, raising=False)
    monkeypatch.setattr(
        service.sys, "executable", "/Users/John Doe/venv/bin/python"
    )
    plist = service._launchd_plist_content()
    assert "<string>/Users/John Doe/venv/bin/python</string>" in plist
    assert "<string>-m</string>" in plist
    assert "<string>bridge.cli</string>" in plist
    assert f"<string>{service._LAUNCHD_LABEL}</string>" in plist
    assert f"<string>{REPO_ROOT}</string>" in plist


def test_service_names_do_not_collide_with_existing_deployments():
    """Production boxes already run a hand-written aipg-media-worker.service and
    the text worker's grid-inference-worker; our artifacts must be distinct."""
    assert service._SERVICE_NAME not in ("aipg-media-worker", "grid-inference-worker")
    assert service._WIN_APP_NAME != "GridInferenceWorker"
    assert service._LAUNCHD_LABEL != "io.aipowergrid.worker"


def test_cli_dispatches_service_flags(monkeypatch):
    import bridge
    import bridge.cli as cli

    calls = []
    fake_service = types.SimpleNamespace(
        status=lambda: calls.append("status"),
        install=lambda start=True, restart_on_crash=False: calls.append("install") or True,
        uninstall=lambda: calls.append("uninstall") or True,
        schedule_start=lambda: calls.append("schedule"),
    )
    # `from . import service` resolves through both the module cache and the
    # already-imported package attribute — patch the two coherently.
    monkeypatch.setitem(sys.modules, "bridge.service", fake_service)
    monkeypatch.setattr(bridge, "service", fake_service, raising=False)
    for flag, expected in [
        (["--service-status"], "status"),
        (["--install-service"], "install"),
        (["--uninstall-service"], "uninstall"),
    ]:
        calls.clear()
        monkeypatch.setattr(sys, "argv", ["comfy-bridge", *flag])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert expected in calls


def test_windows_prefers_windowless_python(win, monkeypatch, tmp_path):
    """The Run value must record pythonw.exe when the venv has it — python.exe
    is a console binary and would open a visible window at every sign-in."""
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"")
    (scripts / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(service.sys, "executable", str(scripts / "python.exe"))
    service.install(verbose=False)
    cmd = win.values[service._WIN_APP_NAME]
    assert "pythonw.exe" in cmd
    assert cmd.startswith('"') and cmd.endswith(" -m bridge.cli")


def test_systemd_execstart_quotes_paths_with_spaces(monkeypatch):
    monkeypatch.setattr(service.sys, "executable", "/opt/my venv/bin/python")
    unit = service._systemd_unit_content()
    assert 'ExecStart="/opt/my venv/bin/python" -m bridge.cli' in unit


def test_launchd_plist_survives_xml_special_chars(monkeypatch):
    import plistlib

    monkeypatch.setattr(service.sys, "frozen", False, raising=False)
    monkeypatch.setattr(service.sys, "executable", "/Users/a&b <c>/venv/bin/python")
    parsed = plistlib.loads(service._launchd_plist_content().encode())
    assert parsed["ProgramArguments"][0] == "/Users/a&b <c>/venv/bin/python"
    assert parsed["ProgramArguments"][1:] == ["-m", "bridge.cli"]
    assert parsed["KeepAlive"] is True


# ---------------------------------------------------------------------------
# --restart-on-crash (Windows scheduled-task watchdog)
# ---------------------------------------------------------------------------

def _task_xml(win):
    return win.schtasks.tasks[service._WIN_TASK_NAME]


def test_restart_on_crash_registers_per_user_watchdog_task(win, monkeypatch):
    monkeypatch.setenv("USERDOMAIN", "AzureAD")
    monkeypatch.setenv("USERNAME", "Jo Smith")
    monkeypatch.setattr(
        service.sys, "executable", r"C:\Users\Jo Smith\venv\Scripts\python.exe"
    )
    assert service.install(verbose=False, restart_on_crash=True) is True
    xml = _task_xml(win)
    # Per-user logon trigger: an any-user one needs an administrator.
    assert "<LogonTrigger>" in xml and "<UserId>AzureAD\\Jo Smith</UserId>" in xml
    # Watchdog: repeat forever; skip while running; no 72h execution limit.
    assert f"<Interval>PT{service._WIN_WATCHDOG_MINUTES}M</Interval>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml
    assert "<Arguments>-m bridge.cli</Arguments>" in xml
    assert f"<WorkingDirectory>{REPO_ROOT}</WorkingDirectory>" in xml
    # Started immediately through the task itself.
    assert win.schtasks.runs == [service._WIN_TASK_NAME]
    assert service.is_installed() is True


def test_task_xml_is_well_formed_and_escapes_paths(win, monkeypatch):
    from xml.dom import minidom

    monkeypatch.setattr(service.sys, "executable", r"C:\a&b <c>\Scripts\python.exe")
    xml = service._win_task_xml(start_boundary="2026-01-01T00:00:00")
    assert r"<Command>C:\a&amp;b &lt;c&gt;\Scripts\python.exe</Command>" in xml
    minidom.parseString(xml.split("?>", 1)[1])


def test_restart_on_crash_does_not_start_a_second_bridge(win, monkeypatch):
    monkeypatch.setattr(
        service, "_probe_bridge", lambda timeout=2.0: {"worker_running": True}
    )
    assert service.install(verbose=False, restart_on_crash=True) is True
    assert win.schtasks.runs == []


def test_install_modes_replace_each_other(win):
    service.install(verbose=False)
    assert service._WIN_APP_NAME in win.values
    service.install(verbose=False, restart_on_crash=True)
    assert service._WIN_APP_NAME not in win.values  # no double start at sign-in
    assert service._WIN_TASK_NAME in win.schtasks.tasks
    service.install(verbose=False)
    assert service._WIN_TASK_NAME not in win.schtasks.tasks
    assert service._WIN_APP_NAME in win.values


def test_uninstall_removes_either_mode(win, capsys):
    service.install(verbose=False, restart_on_crash=True)
    assert service.uninstall() is True
    assert service._WIN_TASK_NAME not in win.schtasks.tasks
    assert "removed" in capsys.readouterr().out
    assert service.uninstall() is False
    assert "was not installed" in capsys.readouterr().out


def test_task_creation_failure_is_reported(win, monkeypatch, capsys):
    monkeypatch.setattr(
        service, "_schtasks", lambda *a: (1, "ERROR: Access is denied.")
    )
    assert service.install(restart_on_crash=True) is False
    assert "Access is denied" in capsys.readouterr().out


def test_restart_on_crash_is_a_note_on_other_platforms(monkeypatch, capsys):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "_linux_install", lambda verbose, start: True)
    assert service.install(restart_on_crash=True) is True
    assert "already restarts" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --service-status: is the bridge actually running?
# ---------------------------------------------------------------------------

RUNNING = {
    "worker_running": True, "worker_error": None,
    "advertised": ["FLUX.2 Klein 4B FP8", "sdxl"],
}


def test_status_reports_a_running_connected_bridge(win, monkeypatch, capsys):
    service.install(verbose=False)
    monkeypatch.setattr(service, "_probe_bridge", lambda timeout=2.0: RUNNING)
    assert service.status() == 0
    out = capsys.readouterr().out
    assert "Run key" in out
    assert "Bridge: running" in out and "advertising 2 model(s)" in out


def test_status_shows_the_waiting_message(win, monkeypatch, capsys):
    service.install(verbose=False)
    waiting = dict(RUNNING, advertised=[], worker_error="Waiting for ComfyUI at x")
    monkeypatch.setattr(service, "_probe_bridge", lambda timeout=2.0: waiting)
    assert service.status() == 0
    assert "Waiting for ComfyUI at x" in capsys.readouterr().out


def test_status_flags_a_crashed_bridge_with_its_last_log_lines(
    win, monkeypatch, tmp_path, capsys
):
    (tmp_path / "bridge-service.log").write_text(
        "early line\n[ERROR] Fatal error: boom\n", encoding="utf-8"
    )
    monkeypatch.setattr(service, "REPO_ROOT", tmp_path)
    service.install(verbose=False)
    assert service.status() == 1
    out = capsys.readouterr().out
    assert "NOT running" in out
    assert "next sign-in" in out
    assert "Fatal error: boom" in out


def test_status_in_restart_mode_mentions_the_watchdog(
    win, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(service, "REPO_ROOT", tmp_path)  # no log file
    service.install(verbose=False, restart_on_crash=True)
    capsys.readouterr()
    assert service.status() == 1
    out = capsys.readouterr().out
    assert "restart-on-crash" in out
    assert f"within {service._WIN_WATCHDOG_MINUTES} minutes" in out


def test_status_when_not_installed_still_exits_zero(win, capsys):
    assert service.status() == 0
    assert "not installed" in capsys.readouterr().out


def test_status_notices_a_foreign_program_on_the_port(win, monkeypatch, capsys):
    service.install(verbose=False)
    monkeypatch.setattr(service, "_probe_bridge", lambda timeout=2.0: {})
    assert service.status() == 1
    assert "not a Comfy Bridge" in capsys.readouterr().out


def test_probe_classifies_answers(monkeypatch):
    import io
    import urllib.request

    def answer(body):
        return lambda url, timeout: io.BytesIO(body)

    monkeypatch.setattr(urllib.request, "urlopen", answer(b'{"worker_running": true}'))
    assert service._probe_bridge() == {"worker_running": True}
    monkeypatch.setattr(urllib.request, "urlopen", answer(b"<html>other app</html>"))
    assert service._probe_bridge() == {}

    def refused(url, timeout):
        raise ConnectionRefusedError()

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert service._probe_bridge() is None


def test_cli_restart_on_crash_flag(monkeypatch):
    import bridge
    import bridge.cli as cli

    seen = {}
    fake_service = types.SimpleNamespace(
        install=lambda start=True, restart_on_crash=False: (
            seen.update(rc=restart_on_crash) or True
        ),
        schedule_start=lambda: seen.update(scheduled=True),
        status=lambda: 1,
    )
    monkeypatch.setitem(sys.modules, "bridge.service", fake_service)
    monkeypatch.setattr(bridge, "service", fake_service, raising=False)
    monkeypatch.setattr(cli.sys, "platform", "win32")

    argv = ["comfy-bridge", "--install-service", "--restart-on-crash"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0 and seen == {"rc": True}  # the task starts itself

    monkeypatch.setattr(sys, "argv", ["comfy-bridge", "--restart-on-crash"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2  # only valid with --install-service

    monkeypatch.setattr(sys, "argv", ["comfy-bridge", "--service-status"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1  # installed but not running
