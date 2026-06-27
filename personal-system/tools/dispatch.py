#!/usr/bin/env python3
"""Persona dispatch + addressing.

Resolves which voice handles a turn and keeps a sticky "active voice".
Precedence: reply-to > leading glyph/name > sticky active voice > default (Donna).
Pure/injectable so it tests without a daemon or DB.
"""
import os
import re
import json
import pathlib
import argparse
import importlib.util
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
PERSONAS_DIR = ROOT / "personas"
DEFAULT_STATE_PATH = ROOT / "_shared" / "_state" / "active_voice.json"
DEFAULT_PERSONA = "donna"

PERSONAS = {
    "donna": {"name": "Donna", "glyph": "💁‍♀️", "aliases": [],
              "descriptor": "your PA, in the truest sense."},
    "nike":  {"name": "Nike",  "glyph": "💪", "aliases": [],
              "descriptor": "your trainer — drive, the body, the win."},
    "esme":  {"name": "Esme",  "glyph": "🌱", "aliases": [],
              "descriptor": "your coach and therapist — growth and self-worth."},
    "bodhi": {"name": "Bodhi", "glyph": "🗻", "aliases": [],
              "descriptor": "your contemplative — stillness, awe, meaning."},
}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_address(text):
    """Return (persona_id_or_None, cleaned_text). Detects a leading glyph or Name prefix."""
    t = text.strip()
    for pid, p in PERSONAS.items():
        if t.startswith(p["glyph"]):
            return pid, t[len(p["glyph"]):].lstrip(" ,:‍")
    m = re.match(r"^([A-Za-z]+)[\s,:]+(.*)$", t, re.DOTALL)
    if m:
        name = m.group(1).lower()
        for pid, p in PERSONAS.items():
            if name == p["name"].lower() or name in p["aliases"]:
                return pid, m.group(2).strip()
    return None, text


def load_state(state_path=DEFAULT_STATE_PATH):
    try:
        with open(state_path) as f:
            return json.load(f).get("active")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(persona, state_path=DEFAULT_STATE_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(state_path)), exist_ok=True)
    with open(state_path, "w") as f:
        json.dump({"active": persona, "updated_at": now_iso()}, f)


def route(text, reply_to=None, state_path=DEFAULT_STATE_PATH):
    prior = load_state(state_path) or DEFAULT_PERSONA
    clean = text
    if reply_to and reply_to in PERSONAS:
        persona = reply_to
    else:
        addressed, cleaned = parse_address(text)
        if addressed:
            persona, clean = addressed, cleaned
        else:
            persona = prior
    save_state(persona, state_path)
    return {"persona": persona, "text": clean, "switched": persona != prior}


def assemble_context(persona_id, recall=""):
    p = PERSONAS[persona_id]
    pf = PERSONAS_DIR / persona_id / "PERSONA.md"
    header = pf.read_text() if pf.exists() else \
        f"You are {p['name']} {p['glyph']}. {p['descriptor']}"
    return header + ("\n\n" + recall if recall else "")


def _recall_for(persona_id):
    try:
        spec = importlib.util.spec_from_file_location(
            "recall_bootstrap", ROOT / "tools" / "recall_bootstrap.py")
        rb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rb)
        return rb.bootstrap(persona_id)
    except Exception:
        return ""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dispatch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("route")
    r.add_argument("text")
    r.add_argument("--reply-to")
    c = sub.add_parser("context")
    c.add_argument("persona")
    args = ap.parse_args(argv)
    if args.cmd == "route":
        print(json.dumps(route(args.text, reply_to=args.reply_to)))
    elif args.cmd == "context":
        print(assemble_context(args.persona, recall=_recall_for(args.persona)))


if __name__ == "__main__":
    main()
