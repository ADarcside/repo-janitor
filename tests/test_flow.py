"""Full nightly flow against a fake GitHub (local git remotes + stub `gh`)."""

import json
import os
import subprocess
from pathlib import Path

import janitor.run as run

FAKE_GH = r"""#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
case "$1 $2" in
  "api user") echo '{"login":"ADarcside","id":42}' ;;
  "pr list") echo '[]' ;;
  "pr create") echo "https://github.com/$4/pull/1" ;;
  "pr merge") exit 0 ;;
  "issue list") exit 0 ;;
  "issue create") echo "https://github.com/ADarcside/repo-janitor/issues/7" ;;
  "label create"|"issue comment") exit 0 ;;
  *)
    if [[ "$2" == */commits/* ]]; then echo "merged-sha"; else
    echo '{"default_branch":"main","archived":false,"fork":false}'; fi ;;
esac
"""


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _commit_all(src, msg):
    _git(src, "add", ".")
    _git(src, "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-qm", msg)
    _git(src, "push", "-q", "origin", "main")


def _make_remote(root: Path, name: str, files: dict) -> Path:
    bare = root / "remote" / "ADarcside" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "--bare", "-b", "main", str(bare))
    src = root / "src" / name
    src.mkdir(parents=True)
    _git(src, "init", "-q", "-b", "main")
    _git(src, "remote", "add", "origin", str(bare))
    for rel, text in files.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    _commit_all(src, "init")
    return src


def test_nightly_flow(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    log = tmp_path / "gh.log"

    passing_test = "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    srcs = {
        "small": _make_remote(tmp_path, "small", {"a.py": "x = 1\n"}),
        "big": _make_remote(tmp_path, "big", {"keep.py": "k = 1\n"}),
        "tidy": _make_remote(tmp_path, "tidy", {"t.py": "z = 1\n"}),
        "tested": _make_remote(tmp_path, "tested", {"calc.py": "def add(a, b):\n    return a + b\n",
                                                    "tests/test_calc.py": passing_test}),
        "broken": _make_remote(tmp_path, "broken", {"calc.py": "def add(a, b):\n    return a - b\n",
                                                    "tests/test_calc.py": passing_test}),
    }

    cfg = tmp_path / "config.yml"
    cfg.write_text("owner: ADarcside\nrepos: [small, big, tidy, tested, broken]\n"
                   "author_email: me@example.com\nprettier: {enabled: false}\n")
    monkeypatch.setattr(run, "CONFIG_FILE", cfg)
    monkeypatch.setattr(run, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.setenv("JANITOR_TOKEN", "secret-token")
    monkeypatch.setenv("JANITOR_GIT_BASE", str(tmp_path / "remote"))
    monkeypatch.setenv("JANITOR_VENV_SYSTEM_SITE", "1")
    monkeypatch.setenv("GITHUB_REPOSITORY", "ADarcside/repo-janitor")
    monkeypatch.setenv("GITHUB_TOKEN", "actions-token")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    # Night 1: nothing recorded yet -> only start watching, no PRs.
    assert run.main([]) == 0
    calls = log.read_text()
    assert "pr create" not in calls
    assert len(json.loads((tmp_path / "state.json").read_text())) == 5
    assert "issue comment 7 -R ADarcside/repo-janitor" in calls  # nightly summary even when quiet

    # You push some messy code during the day.
    (srcs["small"] / "a.py").write_text("x=1\ny =2\n")
    for i in range(12):
        (srcs["big"] / f"m{i}.py").write_text("y=[1,2 ,3]\n" * 3)
    (srcs["tidy"] / "u.py").write_text("w = 2\n")
    (srcs["tested"] / "calc.py").write_text("def add(a,b):\n    return a+b\n")
    (srcs["broken"] / "calc.py").write_text("def add(a,b):\n    return a-b\n")
    for s in srcs.values():
        _commit_all(s, "work")

    # Night 2: clean only what changed.
    log.write_text("")
    assert run.main([]) == 0
    calls = log.read_text()
    state = json.loads((tmp_path / "state.json").read_text())

    # small: PR without needs-review, merged
    assert "pr merge https://github.com/ADarcside/small/pull/1" in calls
    assert state["ADarcside/small"] == "merged-sha"
    # big: needs-review, never merged
    big_part = calls.split("pr create -R ADarcside/big", 1)[1].split("pr create -R", 1)[0]
    assert "--label needs-review" in big_part
    assert "pr merge https://github.com/ADarcside/big" not in calls
    # tidy: nothing to do
    assert "pr create -R ADarcside/tidy" not in calls
    # tested: tests passed before and after -> merged, and the PR says so
    tested_part = calls.split("pr create -R ADarcside/tested", 1)[1]
    assert "pytest passed before and after" in tested_part
    assert "pr merge https://github.com/ADarcside/tested/pull/1" in calls
    # broken: tests already failing -> skipped, not cleaned, state not advanced, flagged to you
    assert "pr create -R ADarcside/broken" not in calls
    assert "blocked" in calls and "tests were already failing" in calls
    assert "need you" in calls

    # commits pushed as you, with the configured email
    big_remote = tmp_path / "remote" / "ADarcside" / "big.git"
    branch = _git(big_remote, "branch", "--list", "janitor/*").strip()
    assert _git(big_remote, "log", "-1", "--format=%an <%ae>", branch).strip() == "ADarcside <me@example.com>"
    # only the changed files were cleaned in big (keep.py was never touched)
    changed = _git(big_remote, "diff", "--name-only", f"main..{branch}").split()
    assert "keep.py" not in changed and len(changed) == 12
    assert "secret-token" not in calls
