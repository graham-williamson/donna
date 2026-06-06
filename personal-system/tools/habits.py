#!/usr/bin/env python3
"""Atomic-Habits engine — identity-based habits with streaks.

Source of truth: habits.json (in _shared/_state, gitignored, Time-Machine backed).
Each habit carries an IDENTITY ("I am someone who...") because Atomic Habits are
won at the level of who you're becoming, not the outcome. Completion is logged
per-day; streaks make the chain visible (don't break the chain).
"""
import os
import json
import pathlib
import argparse
from datetime import date, datetime, timezone, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_HABITS_PATH = os.environ.get("HABITS_PATH", str(ROOT / "_shared" / "_state" / "habits.json"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today():
    return date.today().isoformat()


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"habits": []}


def _save(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def add_habit(name, identity, cue="", owner="bodhi", cadence="daily", habits_path=None):
    path = habits_path or DEFAULT_HABITS_PATH
    data = _load(path)
    hid = max([h["id"] for h in data["habits"]], default=0) + 1
    habit = {"id": hid, "name": name, "identity": identity, "cue": cue,
             "owner": owner, "cadence": cadence, "created_at": now_iso(), "log": []}
    data["habits"].append(habit)
    _save(path, data)
    return habit


def _find(data, hid):
    for h in data["habits"]:
        if h["id"] == hid:
            return h
    raise KeyError(hid)


def log_done(hid, day=None, habits_path=None):
    path = habits_path or DEFAULT_HABITS_PATH
    data = _load(path)
    h = _find(data, hid)
    d = day or _today()
    if d not in h["log"]:
        h["log"].append(d)
        h["log"].sort()
    _save(path, data)
    return h


def streak(hid, today=None, habits_path=None):
    path = habits_path or DEFAULT_HABITS_PATH
    h = _find(_load(path), hid)
    done = set(h["log"])
    cur = date.fromisoformat(today or _today())
    if cur.isoformat() not in done:  # today not yet done — streak can still be alive
        cur = cur - timedelta(days=1)
    s = 0
    while cur.isoformat() in done:
        s += 1
        cur = cur - timedelta(days=1)
    return s


def list_habits(habits_path=None):
    return _load(habits_path or DEFAULT_HABITS_PATH)["habits"]


def due_today(today=None, habits_path=None):
    d = today or _today()
    return [h for h in list_habits(habits_path) if d not in h["log"]]


# The first agreed habit — Graham's daily calm when he wakes (Bodhi's domain).
MORNING_CALM = {
    "name": "Morning Calm",
    "identity": "I am someone who begins each day in stillness",
    "cue": "when I wake",
    "owner": "bodhi",
}


def seed_defaults(habits_path=None):
    path = habits_path or DEFAULT_HABITS_PATH
    existing = {h["name"] for h in list_habits(path)}
    created = []
    if MORNING_CALM["name"] not in existing:
        created.append(add_habit(MORNING_CALM["name"], MORNING_CALM["identity"],
                                 cue=MORNING_CALM["cue"], owner=MORNING_CALM["owner"],
                                 habits_path=path))
    return created


def main(argv=None):
    ap = argparse.ArgumentParser(prog="habits")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("identity")
    a.add_argument("--cue", default="")
    a.add_argument("--owner", default="bodhi")
    dn = sub.add_parser("done")
    dn.add_argument("id", type=int)
    st = sub.add_parser("streak")
    st.add_argument("id", type=int)
    sub.add_parser("list")
    sub.add_parser("seed")
    sub.add_parser("due")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        print(json.dumps(add_habit(args.name, args.identity, args.cue, args.owner)))
    elif args.cmd == "done":
        print(json.dumps(log_done(args.id)))
    elif args.cmd == "streak":
        print(json.dumps({"id": args.id, "streak": streak(args.id)}))
    elif args.cmd == "list":
        print(json.dumps(list_habits(), indent=2))
    elif args.cmd == "seed":
        print(json.dumps(seed_defaults()))
    elif args.cmd == "due":
        print(json.dumps(due_today(), indent=2))


if __name__ == "__main__":
    main()
