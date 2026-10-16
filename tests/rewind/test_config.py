from __future__ import annotations

import os

from rewind.config import FlowRef, load_dotenv


def test_later_lines_win_and_blank_lines_do_not_mask(tmp_path, monkeypatch):
    for name in ("REWIND_HASH_KEY", "GITLAB_URL", "KEEP"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("REWIND_HASH_KEY=\nGITLAB_URL=\n# comment\nexport KEEP='quoted value'\nREWIND_HASH_KEY=abc123\n")
    load_dotenv(env)
    assert os.environ["REWIND_HASH_KEY"] == "abc123"  # generated value appended later is used
    assert "GITLAB_URL" not in os.environ  # blank template line doesn't mask the default
    assert os.environ["KEEP"] == "quoted value"


def test_real_environment_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "from-shell")
    (tmp_path / ".env").write_text("GITLAB_TOKEN=from-file\n")
    load_dotenv(tmp_path / ".env")
    assert os.environ["GITLAB_TOKEN"] == "from-shell"
    monkeypatch.delenv("GITLAB_TOKEN")


def test_flow_refs():
    assert FlowRef.parse("none").enabled is False
    assert str(FlowRef.parse("catalog:7")) == "catalog:7"
