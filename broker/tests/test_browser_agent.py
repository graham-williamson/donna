from __future__ import annotations

import json

from broker import browser_agent as ba


SANITISED = {"source": "webpage", "trust": "untrusted",
             "url": "https://account.everyoneactive.com/x",
             "elements": [{"ref": "r1", "role": "button", "text": "Confirm booking", "editable": False}]}


def test_returns_parsed_action():
    def complete(system, user):
        return json.dumps({"kind": "click", "ref": "r1", "expected_text": "Confirm booking"})
    agent = ba.Agent(goal="book a court", phase="execute", complete=complete)
    a = agent.next(SANITISED)
    assert a["kind"] == "click" and a["ref"] == "r1"


def test_malformed_model_output_becomes_give_up():
    agent = ba.Agent(goal="x", phase="plan", complete=lambda s, u: "i refuse to output json")
    a = agent.next(SANITISED)
    assert a["kind"] == "give_up"


def test_model_call_raising_becomes_give_up():
    # The headless `claude -p` subprocess can fail (timeout, non-zero exit). The
    # wrapper must fail closed to give_up, never propagate the exception or guess.
    def complete(system, user):
        raise RuntimeError("claude -p subprocess exploded")
    a = ba.Agent(goal="x", phase="execute", complete=complete).next(SANITISED)
    assert a["kind"] == "give_up"


def test_agent_feeds_prior_actions_back_to_avoid_loops():
    # The agent must remember what it did. Step 1 has no history; step 2's prompt
    # must list step 1's action so the model progresses instead of repeating it.
    prompts = []

    def complete(system, user):
        prompts.append(user)
        n = len(prompts)
        return json.dumps({"kind": "type", "ref": f"el{n}",
                           "expected_label": "", "text": "x"})

    agent = ba.Agent(goal="log in", phase="execute", complete=complete)
    agent.next(SANITISED)
    agent.next(SANITISED)
    assert "not taken any action" in prompts[0]
    assert "ALREADY completed" in prompts[1]
    assert '"ref": "el1"' in prompts[1]   # step 1's action fed back into step 2


def test_invalid_json_in_braces_becomes_give_up():
    # A `{...}` span that is not valid JSON makes json.loads raise; fail closed.
    a = ba.Agent(goal="x", phase="plan",
                 complete=lambda s, u: "here you go: {kind: click, not json}").next(SANITISED)
    assert a["kind"] == "give_up"


def test_out_of_vocab_action_becomes_give_up():
    agent = ba.Agent(goal="x", phase="plan", complete=lambda s, u: json.dumps({"kind": "evaluate_js"}))
    a = agent.next(SANITISED)
    assert a["kind"] == "give_up"


def test_system_prompt_marks_page_untrusted():
    captured = {}
    def complete(system, user):
        captured["system"] = system
        return json.dumps({"kind": "read"})
    ba.Agent(goal="x", phase="plan", complete=complete).next(SANITISED)
    assert "untrusted" in captured["system"].lower()
    assert "not" in captured["system"].lower() and "instruction" in captured["system"].lower()
