"""The gate as a CI job: `rewind gate` on a real git checkout, no GitLab API involved."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rewind.cli.main import app
from rewind.localgit import LocalGit
from tests.service.helpers import PRICING


def git(repo: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
           "HOME": str(repo), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          env=env).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "app").mkdir()
    (r / "app" / "pricing.py").write_text(PRICING)
    (r / "README.md").write_text("# shop\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    return r


def change(repo: Path, files: dict[str, str | None], rename: tuple[str, str] | None = None) -> tuple[str, str]:
    base = git(repo, "rev-parse", "HEAD")
    if rename:
        (repo / rename[1]).parent.mkdir(parents=True, exist_ok=True)
        git(repo, "mv", *rename)
    for path, content in files.items():
        p = repo / path
        if content is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")
    return base, git(repo, "rev-parse", "HEAD")


def run_gate(repo: Path, base: str, head: str, *extra: str):
    result = CliRunner().invoke(app, ["gate", "--base", base, "--head", head, "--repo", str(repo),
                                      "--report", str(repo / "r.json"), "--junit", str(repo / "r.xml"), *extra])
    return result, json.loads((repo / "r.json").read_text())


def test_flagged_change_is_allowed(repo):
    flagged = "def new_pricing_total(items):\n    return sum(items) * 9 // 10"
    base, head = change(repo, {"app/pricing.py": PRICING.replace(
        "def new_pricing_total(items):\n    return sum(items)", flagged)})
    result, report = run_gate(repo, base, head)
    assert result.exit_code == 0, result.output
    assert report["verdict"] == "allow" and "can be undone" in result.output
    assert 'tests="1" failures="0"' in (repo / "r.xml").read_text()


def test_external_call_blocks_the_job_with_the_line(repo):
    base, head = change(repo, {"app/notify.py": "import httpx\n\n\ndef send():\n    httpx.post('https://x')\n"})
    result, report = run_gate(repo, base, head)
    assert result.exit_code == 1
    f = report["findings"][0]
    assert f["rule_id"] == "external-side-effect" and f["path"] == "app/notify.py" and f["line"] == 1
    assert "external-side-effect" in (repo / "r.xml").read_text()


def test_reroute_warns_by_default_and_fails_when_asked(repo):
    base, head = change(repo, {"app/pricing.py": PRICING.replace(
        "def legacy_total(items):\n    return sum(items)", "def legacy_total(items):\n    return sum(items) - 1")})
    result, report = run_gate(repo, base, head)
    assert result.exit_code == 0 and report["verdict"] == "reroute"
    assert "how to make it undoable" in result.output
    result, _ = run_gate(repo, base, head, "--fail-on", "reroute")
    assert result.exit_code == 1


def test_protected_path_and_rename(repo):
    base, head = change(repo, {".gitlab-ci.yml": "stages: [x]\n"}, rename=("README.md", "docs/README.md"))
    result, report = run_gate(repo, base, head)
    assert result.exit_code == 1 and report["findings"][0]["rule_id"] == "protected-path"
    diffs = LocalGit(repo).compare(base, head)["diffs"]
    renamed = next(d for d in diffs if d["renamed_file"])
    assert (renamed["old_path"], renamed["new_path"]) == ("README.md", "docs/README.md")


def test_localgit_reads_files_at_refs(repo):
    base, head = change(repo, {"README.md": None})
    g = LocalGit(repo)
    assert g.get_file_raw("README.md", base) == b"# shop\n"
    assert g.get_file_raw("README.md", head) is None
    d = g.compare(base, head)["diffs"][0]
    assert d["deleted_file"] and d["old_path"] == "README.md"


def test_unusual_paths_are_read_verbatim(repo):
    base, head = change(repo, {"app/café notify.py": "import httpx\n"})
    d = LocalGit(repo).compare(base, head)["diffs"][0]
    assert d["new_path"] == "app/café notify.py" and "+import httpx" in d["diff"]
    result, report = run_gate(repo, base, head)
    assert result.exit_code == 1 and report["findings"][0]["path"] == "app/café notify.py"


def test_policy_comes_from_the_base_so_a_change_cant_relax_it(repo):
    (repo / "rewind").mkdir()
    (repo / "rewind" / "policy.yaml").write_text((Path(__file__).parents[2] / "rewind" / "policy.yaml").read_text())
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "policy")
    relaxed = "version: 1\nclassify:\n  - {kind: '*', class: irreversible, reason: x}\ngate: {rules: []}\n"
    base, head = change(repo, {"rewind/policy.yaml": relaxed, "app/notify.py": "import httpx\n"})
    result, report = run_gate(repo, base, head, "--policy", str(repo / "rewind" / "policy.yaml"))
    assert report["verdict"] == "allow"  # trusting the head's file: the change disabled its own check
    result, report = run_gate(repo, base, head, "--policy", "rewind/policy.yaml", "--policy-ref", base)
    assert result.exit_code == 1 and {f["rule_id"] for f in report["findings"]} >= {"external-side-effect"}
