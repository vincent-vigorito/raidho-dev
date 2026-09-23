"""RAIDHO_JOURNAL=1 esplicito: sessione headless ma umana (pannelli Agents)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import journal_policy  # noqa: E402


def test_optin_overrides_sdk_entrypoint():
    assert journal_policy.is_programmatic({"RAIDHO_JOURNAL": "1"}, "sdk-cli", 1, 5.0, "other") is None


def test_optin_still_skips_empty_session():
    assert journal_policy.is_programmatic({"RAIDHO_JOURNAL": "1"}, "sdk-cli", 0, 5.0, "other") == "no-user-messages"


def test_default_still_skips_sdk():
    assert journal_policy.is_programmatic({}, "sdk-cli", 3, 600.0, "exit") == "entrypoint:sdk-cli"


def test_optout_wins():
    assert journal_policy.is_programmatic({"RAIDHO_JOURNAL": "0"}, "cli", 9, 900.0, "exit") == "env:RAIDHO_JOURNAL=0"
