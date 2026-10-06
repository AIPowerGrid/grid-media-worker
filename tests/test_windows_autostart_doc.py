"""docs/windows-autostart.md commands were verified on a non-admin Windows 11
shell; pin the properties that made them work so an edit cannot quietly
reintroduce the broken forms."""
import re

from bridge.config import REPO_ROOT

DOC = (REPO_ROOT / "docs" / "windows-autostart.md").read_text(encoding="utf-8")
BLOCKS = re.findall(r"```powershell\n(.*?)```", DOC, re.S)
TASK_BLOCKS = [b for b in BLOCKS if "Register-ScheduledTask" in b]


def test_no_admin_only_schtasks_logon_command():
    # schtasks /SC ONLOGON registers an any-user trigger: "Access is denied"
    # from a normal prompt, even with /RU.
    assert not any("schtasks /Create" in b for b in BLOCKS)


def test_task_commands_are_per_user_and_unlimited():
    assert len(TASK_BLOCKS) == 2
    for block in TASK_BLOCKS:
        assert "-AtLogOn -User" in block
        # Task Scheduler's default 72h limit would stop a long-running worker.
        assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in block
        assert "-WorkingDirectory" in block


def test_bridge_task_restarts_through_a_watchdog_trigger():
    bridge = next(b for b in TASK_BLOCKS if "GridMediaBridge" in b)
    assert "-RepetitionInterval" in bridge
    assert "-MultipleInstances IgnoreNew" in bridge
    assert "pythonw.exe" in bridge
    # "If the task fails, restart" does not fire when the program exits
    # nonzero; the doc must not promise it as the crash-restart mechanism.
    assert 'enable\n"If the task fails' not in DOC
