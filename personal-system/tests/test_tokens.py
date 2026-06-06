import json
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("tokens", ROOT / "tools" / "tokens.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_summary_from_jsonl(tmp_path):
    j = tmp_path / "s.jsonl"
    rows = [
        {"type": "assistant", "timestamp": "2026-06-05T10:00:00Z", "message": {
            "role": "assistant", "model": "claude-haiku-4-5", "usage": {
                "input_tokens": 100, "output_tokens": 50,
                "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0}}},
        {"type": "assistant", "timestamp": "2026-06-05T10:01:00Z", "message": {
            "role": "assistant", "model": "claude-haiku-4-5", "usage": {
                "input_tokens": 200, "output_tokens": 80,
                "cache_read_input_tokens": 800, "cache_creation_input_tokens": 0}}},
    ]
    j.write_text("\n".join(json.dumps(r) for r in rows))
    tok = load()
    s = tok.summary(recent=50, session_dir=str(tmp_path))
    assert s["turns"] == 2
    assert s["avg_output_per_turn"] == 65
    assert s["avg_context_per_turn"] == 1000           # (1000 + 1000) / 2
    assert s["avg_fresh_input_per_turn"] == 150         # (100 + 200) / 2
    assert s["cache_hit_rate"] == 85                    # 1700 / 2000
    assert s["by_model"]["claude-haiku-4-5"] == 2


def test_summary_empty(tmp_path):
    assert load().summary(session_dir=str(tmp_path)) == {"turns": 0}
