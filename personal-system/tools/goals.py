#!/usr/bin/env python3
"""Daruma goal board — source of truth (goals.json).

Colour = domain = the voice that owns the push. Eye mechanic:
left eye = committed (the system starts working it), right eye = achieved
(moves to won, and logs a win to Esme's evidence ledger). Nothing is deleted.
"""
import os
import json
import pathlib
import argparse
import importlib.util
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_GOALS_PATH = os.environ.get("GOALS_PATH", str(ROOT / "_shared" / "_state" / "goals.json"))

VALID_COLOURS = {"green", "purple", "red", "black", "pink", "gold", "white", "blue"}
COLOUR_OWNER = {
    "green": "nike", "purple": "esme", "red": "shared", "black": "donna",
    "pink": "esme", "gold": "donna", "white": "shared", "blue": "donna",
}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"goals": []}


def _save(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def add_goal(title, colour, why_it_matters="", priority=3, goals_path=None):
    path = goals_path or DEFAULT_GOALS_PATH
    if colour not in VALID_COLOURS:
        raise ValueError(f"bad colour: {colour}")
    data = _load(path)
    gid = max([g["id"] for g in data["goals"]], default=0) + 1
    goal = {"id": gid, "title": title, "colour": colour, "owner": COLOUR_OWNER[colour],
            "priority": priority, "why_it_matters": why_it_matters,
            "daruma_state": "none", "committed_at": None, "achieved_at": None, "evidence": None}
    data["goals"].append(goal)
    _save(path, data)
    return goal


def _find(data, gid):
    for g in data["goals"]:
        if g["id"] == gid:
            return g
    raise KeyError(gid)


def commit_goal(gid, goals_path=None):
    path = goals_path or DEFAULT_GOALS_PATH
    data = _load(path)
    g = _find(data, gid)
    if g["daruma_state"] != "both":
        g["daruma_state"] = "left"
    g["committed_at"] = g["committed_at"] or now_iso()
    _save(path, data)
    return g


def _log_win(text):
    spec = importlib.util.spec_from_file_location("evidence", ROOT / "tools" / "evidence.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    return ev.log_win(text)


def achieve_goal(gid, goals_path=None):
    path = goals_path or DEFAULT_GOALS_PATH
    data = _load(path)
    g = _find(data, gid)
    g["daruma_state"] = "both"
    g["committed_at"] = g["committed_at"] or now_iso()
    g["achieved_at"] = now_iso()
    win = _log_win(f"Achieved goal: {g['title']}")
    g["evidence"] = win.get("id")
    _save(path, data)
    return g


def list_goals(colour=None, owner=None, state=None, goals_path=None):
    path = goals_path or DEFAULT_GOALS_PATH
    gs = _load(path)["goals"]
    if colour:
        gs = [g for g in gs if g["colour"] == colour]
    if owner:
        gs = [g for g in gs if g["owner"] == owner]
    if state:
        gs = [g for g in gs if g["daruma_state"] == state]
    return gs


def main(argv=None):
    ap = argparse.ArgumentParser(prog="goals")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("title")
    a.add_argument("colour")
    a.add_argument("--why", default="")
    a.add_argument("--priority", type=int, default=3)
    c = sub.add_parser("commit")
    c.add_argument("id", type=int)
    ac = sub.add_parser("achieve")
    ac.add_argument("id", type=int)
    ls = sub.add_parser("list")
    ls.add_argument("--colour")
    ls.add_argument("--owner")
    ls.add_argument("--state")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        print(json.dumps(add_goal(args.title, args.colour, args.why, args.priority)))
    elif args.cmd == "commit":
        print(json.dumps(commit_goal(args.id)))
    elif args.cmd == "achieve":
        print(json.dumps(achieve_goal(args.id)))
    elif args.cmd == "list":
        print(json.dumps(list_goals(args.colour, args.owner, args.state), indent=2))


if __name__ == "__main__":
    main()
