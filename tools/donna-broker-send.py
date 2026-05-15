#!/usr/bin/env python3
"""Send a broker request by piping an arbitrary JSON file to donna-broker stdin.

Bypasses the §14.1 hook single-quote constraint (BROKER_CMD_RE requires
[^']* in the JSON slot, which breaks Notion DDL statements that embed
single-quoted option names). The hook allowlist permits this script with
a constrained argv: python3 <this_file> <mode> /tmp/donna-*.json.

Usage: python3 donna-broker-send.py <mode> /tmp/donna-<id>.json
"""
import subprocess
import sys
import json

if len(sys.argv) != 3:
    sys.exit("usage: donna-broker-send.py <mode> <json-file>")

mode = sys.argv[1]
json_path = sys.argv[2]

with open(json_path) as f:
    payload = f.read()

json.loads(payload)  # validate before sending

result = subprocess.run(
    ["sudo", "-u", "donna-broker", "/usr/local/bin/donna-broker", mode],
    input=payload.encode(),
    capture_output=True,
)
sys.stdout.buffer.write(result.stdout)
if result.stderr:
    sys.stderr.buffer.write(result.stderr)
sys.exit(result.returncode)
