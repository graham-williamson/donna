import importlib.util
import pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_add_wish_is_open(tmp_path):
    w = load("wishes").add_wish("see the cherry blossoms in Kyoto",
                                wishes_path=str(tmp_path / "w.json"))
    assert w["status"] == "open" and w["promoted_goal_id"] is None and w["created_at"]


def test_promote_creates_goal_and_links(tmp_path):
    wishes = load("wishes")
    wp, gp = str(tmp_path / "w.json"), str(tmp_path / "g.json")
    w = wishes.add_wish("learn the tea ceremony", wishes_path=wp)
    goal = wishes.promote_wish(w["id"], "purple", wishes_path=wp, goals_path=gp)
    assert goal["title"] == "learn the tea ceremony" and goal["colour"] == "purple"
    promoted = wishes.list_wishes(status="promoted", wishes_path=wp)
    assert promoted[0]["promoted_goal_id"] == goal["id"]


def test_promote_bad_colour_leaves_wish_open(tmp_path):
    wishes = load("wishes")
    wp = str(tmp_path / "w.json")
    w = wishes.add_wish("x", wishes_path=wp)
    with pytest.raises(ValueError):
        wishes.promote_wish(w["id"], "teal", wishes_path=wp,
                            goals_path=str(tmp_path / "g.json"))
    assert wishes.list_wishes(wishes_path=wp)[0]["status"] == "open"


def test_release_archives_not_deletes(tmp_path):
    wishes = load("wishes")
    wp = str(tmp_path / "w.json")
    w = wishes.add_wish("x", wishes_path=wp)
    wishes.release_wish(w["id"], wishes_path=wp)
    assert wishes.list_wishes(wishes_path=wp) == []
    assert wishes.list_wishes(status=None, wishes_path=wp)[0]["status"] == "released"


def test_seed_daru_once(tmp_path):
    wishes = load("wishes")
    wp = str(tmp_path / "w.json")
    assert len(wishes.seed_defaults(wishes_path=wp)) == 1
    assert wishes.seed_defaults(wishes_path=wp) == []
    assert "daru.life" in wishes.list_wishes(wishes_path=wp)[0]["text"]
