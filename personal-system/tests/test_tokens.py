import json
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("tokens", ROOT / "tools" / "tokens.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sessions(tmp_path, rows):
    """Write rows as one JSONL in a dedicated sessions dir (kept separate from
    the markers file so glob('*.jsonl') doesn't pick the markers up)."""
    sd = tmp_path / "sessions"
    sd.mkdir()
    (sd / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    return sd


def test_cli_session_aggregates(tmp_path):
    # No channel origin -> CLI session.
    rows = [
        {"type": "assistant", "timestamp": "2026-06-05T10:00:00Z", "message": {
            "role": "assistant", "id": "a", "model": "claude-opus-4-8", "usage": {
                "input_tokens": 100, "output_tokens": 50,
                "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0}}},
        {"type": "assistant", "timestamp": "2026-06-05T10:01:00Z", "message": {
            "role": "assistant", "id": "b", "model": "claude-opus-4-8", "usage": {
                "input_tokens": 200, "output_tokens": 80,
                "cache_read_input_tokens": 800, "cache_creation_input_tokens": 0}}},
    ]
    tok = load()
    s = tok.summary(recent=50, session_dir=str(_sessions(tmp_path, rows)),
                    markers_path=str(tmp_path / "none.jsonl"))
    assert s["cli"]["turns"] == 2
    assert s["cli"]["avg_output_per_turn"] == 65
    assert s["cli"]["avg_context_per_turn"] == 1000        # (1000 + 1000) / 2
    assert s["cli"]["avg_fresh_input_per_turn"] == 150      # (100 + 200) / 2
    assert s["cli"]["cache_hit_rate"] == 85                 # 1700 / 2000
    assert s["telegram"]["turns"] == 0


def test_telegram_channel_and_agent_attribution(tmp_path):
    # A channel-origin user turn makes the whole session "telegram"; each
    # assistant turn is attributed to the nearest preceding persona marker.
    rows = [
        {"type": "user", "origin": {"kind": "channel"}, "timestamp": "2026-06-06T10:00:00Z",
         "message": {"role": "user",
                     "content": '<channel source="plugin:telegram:telegram">hi</channel>'}},
        {"type": "assistant", "timestamp": "2026-06-06T10:00:05Z", "message": {
            "role": "assistant", "id": "m1", "model": "claude-haiku-4-5", "usage": {
                "input_tokens": 50, "output_tokens": 40,
                "cache_read_input_tokens": 950, "cache_creation_input_tokens": 0}}},
        {"type": "assistant", "timestamp": "2026-06-06T10:01:05Z", "message": {
            "role": "assistant", "id": "m2", "model": "claude-haiku-4-5", "usage": {
                "input_tokens": 50, "output_tokens": 40,
                "cache_read_input_tokens": 950, "cache_creation_input_tokens": 0}}},
    ]
    markers = tmp_path / "turns.jsonl"
    markers.write_text("\n".join([
        json.dumps({"ts": "2026-06-06T10:00:01Z", "persona": "nike"}),
        json.dumps({"ts": "2026-06-06T10:01:00Z", "persona": "esme"}),
    ]))
    tok = load()
    s = tok.summary(recent=50, session_dir=str(_sessions(tmp_path, rows)),
                    markers_path=str(markers))
    assert s["telegram"]["turns"] == 2
    assert s["cli"]["turns"] == 0
    assert s["telegram"]["by_agent"]["nike"]["turns"] == 1
    assert s["telegram"]["by_agent"]["esme"]["turns"] == 1


def test_dedup_and_skip_models(tmp_path):
    # Same message id repeated (streaming) counts once; helper/synthetic skipped.
    rows = [
        {"type": "assistant", "timestamp": "2026-06-05T10:00:00Z", "message": {
            "role": "assistant", "id": "dup", "model": "claude-opus-4-8", "usage": {
                "input_tokens": 100, "output_tokens": 50,
                "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0}}},
        {"type": "assistant", "timestamp": "2026-06-05T10:00:00Z", "message": {
            "role": "assistant", "id": "dup", "model": "claude-opus-4-8", "usage": {
                "input_tokens": 100, "output_tokens": 50,
                "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0}}},
        {"type": "assistant", "timestamp": "2026-06-05T10:00:01Z", "message": {
            "role": "assistant", "id": "x", "model": "claude-api", "usage": {
                "input_tokens": 5, "output_tokens": 5}}},
        {"type": "assistant", "timestamp": "2026-06-05T10:00:02Z", "message": {
            "role": "assistant", "id": "y", "model": "<synthetic>", "usage": {
                "input_tokens": 5, "output_tokens": 5}}},
    ]
    tok = load()
    s = tok.summary(recent=50, session_dir=str(_sessions(tmp_path, rows)),
                    markers_path=str(tmp_path / "none.jsonl"))
    assert s["cli"]["turns"] == 1


def test_summary_empty(tmp_path):
    s = load().summary(session_dir=str(tmp_path), markers_path=str(tmp_path / "none.jsonl"))
    assert s["telegram"]["turns"] == 0 and s["cli"]["turns"] == 0
