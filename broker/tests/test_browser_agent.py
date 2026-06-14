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
