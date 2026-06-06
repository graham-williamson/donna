#!/usr/bin/env python3
"""Verify all four personas route correctly through dispatch (every address form)."""
import os
import tempfile
import importlib.util
import pathlib

PS = pathlib.Path("/Users/grahamwilliamson/donna/personal-system")
spec = importlib.util.spec_from_file_location("dispatch", PS / "tools" / "dispatch.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

cases = [
    ("🌱 I feel like a fraud", "esme"),
    ("💪 what should I train", "nike"),
    ("🗻 what is enough", "bodhi"),
    ("💁‍♀️ book the dentist", "donna"),
    ("Bodhi, the point of it all", "bodhi"),
    ("Esme, I'm anxious", "esme"),
    ("Nike, leg day?", "nike"),
    ("hello there", "donna"),
]

print("Persona routing check:")
ok = True
for text, expected in cases:
    fd, sp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    got = d.route(text, state_path=sp)["persona"]
    os.unlink(sp)
    mark = "OK " if got == expected else "XX "
    if got != expected:
        ok = False
    print(f"  {mark} {text[:36]:36s} -> {got}  (want {expected})")

print("\nALL FOUR ROUTABLE — accessible" if ok else "\nROUTING MISMATCH — needs a fix")
