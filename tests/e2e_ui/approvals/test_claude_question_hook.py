"""Run the real hook and browser, modeling Claude's configured command timeout."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.claude_native.bridge import build_hook_settings, prepare_bridge_dir

from .test_ask_user_question import _FORM, _SUBMIT, _pending_elicitations, _wait_for


@pytest.mark.timeout(90)
@pytest.mark.parametrize("answer_in_ui", [True, False], ids=["web-answer", "hook-timeout"])
def test_claude_question_hook_round_trip(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    answer_in_ui: bool,
) -> None:
    """Answer after ten seconds, or let the hook runner end the wait at thirty seconds."""
    base_url, session_id = seeded_session
    bridge_dir = prepare_bridge_dir(
        session_id,
        bridge_id=f"question-e2e-{uuid.uuid4().hex}",
        workspace=tmp_path,
    )
    settings = build_hook_settings(bridge_dir, ap_server_url=base_url)
    hook = next(
        entry["hooks"][0]
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "AskUserQuestion"
    )
    question = "Which approach should I take?"
    questions = [
        {
            "header": "Scope",
            "question": question,
            "multiSelect": False,
            "options": [
                {"label": "Alpha", "description": "Apply the small wiring fix first."},
                {"label": "Bravo", "description": "Investigate broader changes."},
            ],
        }
    ]
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": questions},
        "permission_mode": "default",
    }
    process = subprocess.Popen(
        shlex.split(hook["command"]),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    result: dict = {}

    def run_hook() -> None:
        """Apply the same process timeout Claude reads from its hook settings."""
        started_at = time.monotonic()
        try:
            result["stdout"], result["stderr"] = process.communicate(
                input=json.dumps(payload), timeout=hook["timeout"]
            )
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
            process.kill()
            result["stdout"], result["stderr"] = process.communicate()
        except Exception as error:
            result["error"] = error
        finally:
            result["elapsed"] = time.monotonic() - started_at

    worker = threading.Thread(target=run_hook, daemon=True)
    worker.start()
    try:
        page.goto(f"{base_url}/c/{session_id}")
        card = page.locator('[data-testid="approval-card"][data-state="pending"]').filter(
            has=page.locator(_FORM)
        )
        expect(card).to_be_visible(timeout=15_000)
        form = card.locator(_FORM)
        expect(form.get_by_text(question, exact=True)).to_be_visible()
        expect(form.get_by_text("Apply the small wiring fix first.", exact=True)).to_be_visible()

        if answer_in_ui:
            page.wait_for_timeout(12_000)
            expect(card).to_be_visible()
            form.get_by_role("radio", name="Alpha").check()
            form.locator(_SUBMIT).click()

        worker.join(timeout=35.0)
        assert not worker.is_alive(), "Question hook exceeded its thirty-second wait budget"
        assert "error" not in result, result
        if answer_in_ui:
            assert process.returncode == 0, result
            assert not result.get("timed_out"), result
            output = json.loads(result["stdout"])["hookSpecificOutput"]
            assert output["hookEventName"] == "PreToolUse"
            assert output["permissionDecision"] == "allow"
            assert output["updatedInput"] == {
                "questions": questions,
                "answers": {question: "Alpha"},
            }
        else:
            assert result.get("timed_out"), result
            assert 29.0 <= result["elapsed"] <= 35.0, result
            assert result["stdout"] == "", result
        _wait_for(lambda: not _pending_elicitations(base_url, session_id), timeout_s=45.0)
    finally:
        if process.poll() is None:
            process.kill()
        worker.join(timeout=5.0)
        shutil.rmtree(bridge_dir)
