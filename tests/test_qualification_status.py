import json
from pathlib import Path

from bridge.profiles.state import profile_digest


ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "bridge" / "profiles" / "ace-step-v1.profile.json"
STATUS_PATH = ROOT / "docs" / "qualification-status.json"


def test_public_qualification_status_matches_the_draft_profile() -> None:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))["profile"]
    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))

    assert status["schema"] == "aipg-media-manager-qualification-status-v1"
    assert status["profile"] == {
        "id": profile["id"],
        "version": profile["version"],
        "digest": profile_digest(profile),
    }
    assert status["runs_per_evidence_set"] == profile["release_qualification"][
        "runs_per_class"
    ]

    required_classes = profile["release_qualification"]["required_classes"]
    assert [item["id"] for item in status["classes"]] == required_classes

    for item in status["classes"]:
        accepted = item["accepted_evidence_sets"]
        required = item["required_evidence_sets"]
        assert isinstance(accepted, int) and accepted >= 0
        assert isinstance(required, int) and required > 0
        assert item["status"] == ("complete" if accepted >= required else "needed")

    assert status["release_ready"] is all(
        item["status"] == "complete" for item in status["classes"]
    )
    assert status["qualification_release"]["tag"].startswith(
        "manager-qualification-v"
    )
    assert status["qualification_release"]["url"].endswith(
        status["qualification_release"]["tag"]
    )
    assert status["participation_url"].startswith("https://github.com/AIPowerGrid/")
    assert status["cohort_url"].startswith("https://github.com/AIPowerGrid/")
    assert status["runbook_url"].startswith("https://github.com/AIPowerGrid/")
