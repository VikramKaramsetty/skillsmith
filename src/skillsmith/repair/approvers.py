"""Human gate implementations. The dashboard approver is WS6's (dashboard/approver.py)."""

from __future__ import annotations

from typing import Tuple


class AutoApprover:
    """Approves everything, but REFUSES to exist for the real arm."""

    def __init__(self, target_env: str):
        if target_env == "real":
            raise ValueError("AutoApprover cannot be used with the real arm; a human must approve")

    def request(self, run_id: str, summary: dict) -> Tuple[bool, str]:
        return True, "auto-approved (non-real env)"


class CliApprover:
    """Terminal prompt. Fallback when the dashboard is down."""

    def request(self, run_id: str, summary: dict) -> Tuple[bool, str]:
        print("\n" + "=" * 70)
        print(f"APPROVAL NEEDED — run {run_id}: {summary.get('title', '')}")
        for key in ("skill", "version", "sim", "safety", "video"):
            if summary.get(key) is not None:
                print(f"  {key:>8}: {summary[key]}")
        if summary.get("diff"):
            print(summary["diff"])
        answer = input("Run this on the arm? [y/N, or type a note to reject] ").strip()
        if answer.lower() in ("y", "yes"):
            return True, ""
        return False, answer if answer.lower() not in ("", "n", "no") else ""

    def confirm(self, run_id: str, summary: dict) -> Tuple[bool, str]:
        """After a real attempt: did the task succeed? y = success (saved), anything else = failure; a
        note after 'n' (e.g. 'n cup slipped while lifting') is passed to Droid."""
        print("\n" + "=" * 70)
        print(f"OUTCOME — run {run_id}: {summary.get('title', '')}")
        answer = input("Did the task succeed? [y/n, optionally 'n <what went wrong>'] ").strip()
        if answer.lower() in ("y", "yes"):
            return True, ""
        note = answer[1:].strip(" :,-") if answer.lower().startswith("n") else answer
        return False, note
