#!/usr/bin/env python3
"""UserPromptSubmit hook — live persona dispatch for the Telegram daemon.

For each inbound Telegram message: route it (glyph/name/reply -> persona, sticky
active voice), then inject that persona's overlay + memory recall as
additionalContext so the session replies AS that voice, opening with its glyph.

SAFETY:
  - Fail-open: ANY error -> emit nothing, exit 0. Never blocks the daemon.
  - Telegram-only: non-Telegram prompts (e.g. a terminal dev session) are a no-op.
"""
import sys
import re
import json
import importlib.util
import pathlib

PS = pathlib.Path("/Users/grahamwilliamson/donna/personal-system")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    prompt = data.get("prompt") or ""
    if 'source="telegram"' not in prompt:
        return 0  # not a Telegram turn — no-op
    try:
        m = re.search(r'<channel[^>]*>(.*)', prompt, re.DOTALL)
        body = m.group(1) if m else prompt
        body = re.sub(r'</channel>\s*$', '', body).strip()

        dispatch = _load("dispatch", PS / "tools" / "dispatch.py")
        routed = dispatch.route(body)
        persona = routed["persona"]
        overlay = dispatch.assemble_context(persona)
        glyph = dispatch.PERSONAS[persona]["glyph"]
        clean = routed.get("text", body)

        # Deterministic capture: a chatty model won't reliably stop to run a log
        # command mid-conversation, so when Graham explicitly says "remember ...",
        # the hook itself writes it to the never-forget floor. No model compliance
        # required. Fail-open.
        captured_note = ""
        if "remember" in clean.lower():
            try:
                pmem = _load("pmem", PS / "tools" / "pmem.py")
                low = clean.lower()
                kwmap = {
                    "worry": ("worri", "anxious", "anxiet", "afraid", "scared",
                              "stress", "fear", "nervous", "dread"),
                    "wins": ("proud", "achieved", "won", "nailed", "smashed", "finished"),
                    "goals": ("goal", "want to", "aim", "plan to"),
                    "self-worth": ("worthy", "fraud", "imposter", "impostor",
                                   "deserve", "not good enough"),
                    "energy": ("tired", "exhausted", "slept", "energy", "sore"),
                    "training": ("workout", "train", "gym", "lift", "session"),
                }
                topics = [t for t, ws in kwmap.items()
                          if any(w in low for w in ws)] or ["notes"]
                pmem.add({"kind": "episodic", "owner": persona, "shared": 1,
                          "content": clean, "topics": topics})
                captured_note = (
                    f"\n\n[SYSTEM: This message has ALREADY been saved to your memory "
                    f"(owner={persona}, topics={topics}) — you do NOT need to run any "
                    f"command. Just acknowledge it warmly and naturally.]"
                )
            except Exception:
                captured_note = ""

        recall = ""
        try:
            rb = _load("recall_bootstrap", PS / "tools" / "recall_bootstrap.py")
            recall = rb.bootstrap(persona)
        except Exception:
            recall = ""

        tools = PS / "tools"
        mem = (
            f"\n\n## Your shared memory — you MUST actually use it, not just mention it\n"
            f"You are memory owner '{persona}'. **Saying 'remembered' is NOT remembering.** "
            f"When Graham asks you to remember something, or shares a worry / win / goal / "
            f"preference / fact worth keeping, you MUST log it in THIS turn by running the "
            f"command below — never just acknowledge it in words.\n"
            f"TO LOG: use the Write tool to create a file /tmp/pmem-<rand>.json containing "
            f'{{"kind":"episodic","owner":"{persona}","shared":1,"content":"<full text to '
            f'remember>","topics":["<topic>"]}}  — then run: '
            f"python3 {tools}/pmem.py add /tmp/pmem-<rand>.json\n"
            f'Topic guide: worries -> "worry"; goals -> "goals"; wins -> "wins"; '
            f'self-worth -> "self-worth"; body/training -> "energy"/"training".\n'
            f"TO RECALL: python3 {tools}/pmem.py recall --topic <topic> --persona {persona}\n"
            f"Keep private/tender things shared=0; wins and shareable facts shared=1. "
            f"Only tell Graham you've remembered AFTER the add command has actually run.\n"
        )
        if persona == "esme":
            mem += f'- Log a win to your evidence ledger: python3 {tools}/evidence.py log "<the win>"\n'
        if persona == "nike":
            mem += f"- Goals: python3 {tools}/goals.py list  |  commit <id>  |  achieve <id>\n"

        ctx = (
            f"## ACTIVE VOICE FOR THIS TURN: {persona} {glyph}\n\n"
            f"You are speaking as this persona for this reply. Open your reply with "
            f"{glyph} and adopt this voice fully. This overrides the default Donna "
            f"voice for this turn. All security and broker rules in CLAUDE.md still "
            f"apply to you unchanged.\n\n"
            f"{overlay}\n\n{recall}{mem}{captured_note}"
        )
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": ctx,
            }
        }))
    except Exception:
        return 0  # fail open — never block the daemon
    return 0


if __name__ == "__main__":
    sys.exit(main())
