#!/usr/bin/env python3
"""Nike's morning check-in (ICM Layer-2 stage) — input plumbing + reply logging.

Nike composes the message at runtime in her voice; this module gathers the local
data she needs and logs Graham's reply back to the memory floor as observations.
"""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def gather_inputs(goals_path=None):
    goals = _mod("goals")
    pmem = _mod("pmem")
    active = [g for g in goals.list_goals(owner="nike", goals_path=goals_path)
              if g["daruma_state"] in ("left", "both")]
    energy = pmem.recall(topic="energy", persona="nike", limit=5)
    return {"active_goals": active, "recent_energy": energy}


def log_response(text, energy=None):
    pmem = _mod("pmem")
    out = [pmem.add({"kind": "observation", "owner": "nike", "content": text,
                     "topics": ["energy", "training"]})]
    if energy:
        out.append(pmem.add({"kind": "observation", "owner": "nike",
                             "content": f"energy level: {energy}", "topics": ["energy"]}))
    return out
