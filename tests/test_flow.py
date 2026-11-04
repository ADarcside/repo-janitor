"""Full nightly flow against a fake GitHub (local git remote + stub `gh`)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import janitor.run as run

FAKE_GH = r"""#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
case "$1 $2" in
  "api user") echo '{"login":"ADarcside","id":42}' ;;
  "pr list") echo '[]' ;;
  "pr create") echo "https://github.com/$4/pull/1" ;;
  "pr merge") exit 0 ;;
  "label create"|"issue create") exit 0 ;;
  *)
    if [[ "$2" == */commits/* ]]; then echo "merged-sha"; else
    echo '{"default_branch":"main","archived":false,"fork":false}'; fi ;;
esac
"""


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _make_remote(root: Path, name: str, files: dict) -> None:
    src = root / "src" / name
    src.mkdir(parents=True)
    for rel, text in files.items():
        (src / rel).write_text(text)
    _git(src, "init", "-q", "-b", "main")
    _git(src, "add", ".")
    _git(src, "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-qm", "init")
    bare = root / "remote" / "ADarcside" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "clone", "-q", "--bare", str(src), str(bare))


def test_nightly_flow(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    log = tmp_path / "gh.log"

    _make_remote(tmp_path, "small", {"a.py": "x=1\n"})
    _make_remote(tmp_path, "big", {f"m{i}.py": "y=[1,2 ,3]\n" * 3 for i in range(12)})
    _make_remote(tmp_path, "tidy", {"t.py": "z = 1\n"})

    cfg = tmp_path / "config.yml"
    cfg.write_text("owner: ADarcside\nrepos: [small, big, tidy]\nprettier: {enabled: false}\n")
    monkeypatch.setattr(run, "CONFIG_FILE", cfg)
    monkeypatch.setattr(run, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.setenv("JANITOR_TOKEN", "secret-token")
    monkeypatch.setenv("JANITOR_GIT_BASE", str(tmp_path / "remote"))
    monkeypatch.setenv("GITHUB_REPOSITORY", "ADarcside/repo-janitor")
    monkeypatch.setenv("GITHUB_TOKEN", "actions-token")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    assert run.main([]) == 0
    calls = log.read_text()
    state = json.loads((tmp_path / "state.json").read_text())

    # small repo: PR opened without needs-review, then merged
    assert "pr create -R ADarcside/small" in calls
    assert "pr merge https://github.com/ADarcside/small/pull/1" in calls
    assert state["ADarcside/small"] == "merged-sha"
    # big repo: PR with needs-review, never merged
    big_part = calls.split("pr create -R ADarcside/big", 1)[1].split("pr create -R", 1)[0]
    assert "--label needs-review" in big_part
    assert calls.count("--label needs-review") == 1
    assert "pr merge https://github.com/ADarcside/big" not in calls
    # tidy repo: nothing to do, no PR
    assert "ADarcside/tidy --base" not in calls
    # you get notified about the big one
    assert "issue create -R ADarcside/repo-janitor" in calls

    # branches were pushed, commits authored as you
    big_remote = tmp_path / "remote" / "ADarcside" / "big.git"
    branch = _git(big_remote, "branch", "--list", "janitor/*").strip()
    author = _git(big_remote, "log", "-1", "--format=%an <%ae>", branch)
    assert author.strip() == "ADarcside <42+ADarcside@users.noreply.github.com>"

    # second run: tidy and small have no new commits, so they're skipped
    log.write_text("")
    assert run.main([]) == 0
    assert "pr create -R ADarcside/tidy" not in log.read_text()
    assert "secret-token" not in log.read_text()
