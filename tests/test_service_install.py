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


@pytest.fixture
def win(monkeypatch):
    fake = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(service.sys, "platform", "win32")
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
        install=lambda start=True: calls.append("install") or True,
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
