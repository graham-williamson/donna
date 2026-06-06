#!/usr/bin/env python3
"""Ema wish wall — deferred dreams (wishes.json).

At a shrine you write a wish on a wooden ema plaque and hang it up. Here a
wish hangs on the wall until you either promote it to a daruma (it becomes a
goal — you commit) or release it. Nothing is deleted.
"""
import os
import json
import pathlib
import argparse
import importlib.util
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_WISHES_PATH = os.environ.get("WISHES_PATH", str(ROOT / "_shared" / "_state" / "wishes.json"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"wishes": []}


def _save(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def add_wish(text, wishes_path=None):
    path = wishes_path or DEFAULT_WISHES_PATH
    data = _load(path)
    wid = max([w["id"] for w in data["wishes"]], default=0) + 1
    wish = {"id": wid, "text": text, "created_at": now_iso(),
            "status": "open", "promoted_goal_id": None}
    data["wishes"].append(wish)
    _save(path, data)
    return wish


def _find(data, wid):
    for w in data["wishes"]:
        if w["id"] == wid:
            return w
    raise KeyError(wid)


def _goals():
    spec = importlib.util.spec_from_file_location("goals", ROOT / "tools" / "goals.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def promote_wish(wid, colour, wishes_path=None, goals_path=None):
    path = wishes_path or DEFAULT_WISHES_PATH
    data = _load(path)
    w = _find(data, wid)
    goal = _goals().add_goal(w["text"], colour, why_it_matters="from the ema wall",
                             goals_path=goals_path)
    w["status"] = "promoted"
    w["promoted_goal_id"] = goal["id"]
    _save(path, data)
    return goal


def release_wish(wid, wishes_path=None):
    path = wishes_path or DEFAULT_WISHES_PATH
    data = _load(path)
    w = _find(data, wid)
    w["status"] = "released"
    _save(path, data)
    return w


def list_wishes(status="open", wishes_path=None):
    ws = _load(wishes_path or DEFAULT_WISHES_PATH)["wishes"]
    return [w for w in ws if status is None or w["status"] == status]


DARU = "Daru (daru.life) — internet-friendly SaaS version of the Daruma Board"


def seed_defaults(wishes_path=None):
    path = wishes_path or DEFAULT_WISHES_PATH
    if any(w["text"] == DARU for w in list_wishes(status=None, wishes_path=path)):
        return []
    return [add_wish(DARU, wishes_path=path)]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wishes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("text")
    p = sub.add_parser("promote")
    p.add_argument("id", type=int)
    p.add_argument("colour")
    r = sub.add_parser("release")
    r.add_argument("id", type=int)
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    sub.add_parser("seed")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        print(json.dumps(add_wish(args.text)))
    elif args.cmd == "promote":
        print(json.dumps(promote_wish(args.id, args.colour)))
    elif args.cmd == "release":
        print(json.dumps(release_wish(args.id)))
    elif args.cmd == "list":
        print(json.dumps(list_wishes(status=None if args.all else "open"), indent=2))
    elif args.cmd == "seed":
        print(json.dumps(seed_defaults()))


if __name__ == "__main__":
    main()
