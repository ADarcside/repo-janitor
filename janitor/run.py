"""Nightly code cleanup for your GitHub repos. See README.md for setup."""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import safety

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.yml"
STATE_FILE = ROOT / "state.json"
OUT_DIR = ROOT / "out"
LABEL = "janitor"
REVIEW_LABEL = "needs-review"
PRETTIER = "prettier@3.3.3"
MAX_FILE_BYTES = 500_000

DEFAULTS = {
    "owner": "",
    "repos": [],
    "author_name": "",
    "author_email": "",
    "timezone": "Asia/Kolkata",
    "small_max_lines": 150,
    "small_max_files": 10,
    "auto_merge_small": True,
    "python": {"remove_unused_imports": True, "formatter": "auto"},
    "prettier": {
        "enabled": True,
        "extensions": [".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".css", ".scss", ".less"],
    },
    "exclude": [],
}

ALWAYS_EXCLUDE = [
    ".git/", "node_modules/", "dist/", "build/", "out/", ".next/", "coverage/",
    ".venv/", "venv/", "env/", "site-packages/", "vendor/", "third_party/",
    "migrations/", "__pycache__/", "*.min.js", "*.min.css", "*.bundle.js",
    "*_pb2.py", "*_pb2_grpc.py", "*.ipynb",
]


class StepError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def _secrets() -> list[str]:
    return [v for k in ("JANITOR_TOKEN", "GITHUB_TOKEN") if (v := os.environ.get(k))]


def redact(text: str) -> str:
    for s in _secrets():
        text = text.replace(s, "***")
    return text


def sh(cmd: list[str], cwd: Path | None = None, env: dict | None = None, check: bool = True):
    r = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()[-600:]
        raise StepError(redact(f"`{' '.join(cmd[:3])}` failed: {msg}"))
    return r


def gh(args: list[str], token: str, cwd: Path | None = None, check: bool = True):
    return sh(["gh", *args], cwd=cwd, env={**os.environ, "GH_TOKEN": token}, check=check)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="surrogateescape")


def write(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8", errors="surrogateescape")


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    user = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def excluded(rel: str, patterns: list[str]) -> bool:
    parts = rel.split("/")
    for pat in patterns:
        if pat.endswith("/"):
            if pat[:-1] in parts[:-1]:
                return True
        elif fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(parts[-1], pat):
            return True
    return False


def filter_files(repo: Path, rels: list[str], cfg: dict) -> list[Path]:
    patterns = ALWAYS_EXCLUDE + list(cfg.get("exclude") or [])
    exts = {".py"} | set(cfg["prettier"]["extensions"] if cfg["prettier"]["enabled"] else [])
    out = []
    for rel in rels:
        p = repo / rel
        if p.suffix not in exts or excluded(rel, patterns):
            continue
        if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            continue
        out.append(p)
    return out


def git_list(repo: Path, args: list[str]) -> list[str]:
    out = sh(["git", *args, "-z"], cwd=repo).stdout
    return [x for x in out.split("\0") if x]


def commit(repo: Path, message: str, author: tuple[str, str]) -> bool:
    if not sh(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo).stdout.strip():
        return False
    sh(["git", "add", "-u"], cwd=repo)
    sh(["git", "-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}",
        "commit", "-q", "-m", message], cwd=repo)
    return True


# ---------------------------------------------------------------- cleanup steps

class PyIndex:
    """Parsed view of every Python file in the repo, used to spot re-exports."""

    def __init__(self, repo: Path, cfg: dict):
        patterns = ALWAYS_EXCLUDE + list(cfg.get("exclude") or [])
        self.files: list[tuple[Path, str, ast.Module | None]] = []
        for rel in git_list(repo, ["ls-files"]):
            if rel.endswith(".py") and not excluded(rel, patterns):
                p = repo / rel
                if p.is_file():
                    text = read(p)
                    self.files.append((p, text, safety._parse(text)))

    def reexport_risk(self, target: Path, names: list[str], src: str) -> str:
        if re.search(r"\b(globals|locals|eval|exec|__import__)\s*\(|sys\.modules|def __getattr__", src):
            return "file looks names up dynamically"
        mod = target.stem
        for path, text, tree in self.files:
            if path == target or mod not in text:
                continue
            if tree is None:
                if any(re.search(rf"\b{re.escape(n)}\b", text) for n in names):
                    return f"{path.name} may import it (could not parse)"
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    src_mod = (node.module or "").split(".")[-1]
                    if src_mod == mod:
                        imported = {a.name for a in node.names}
                        if "*" in imported:
                            return f"{path.name} does `from {mod} import *`"
                        hit = imported & set(names)
                        if hit:
                            return f"{path.name} imports {', '.join(sorted(hit))} from it"
                elif isinstance(node, ast.Attribute) and node.attr in names:
                    if isinstance(node.value, ast.Name) and node.value.id == mod:
                        return f"{path.name} uses {mod}.{node.attr}"
        return ""


KEEP_MARK = "  # noqa: F401 janitor-keep"


def _protect_bare_imports(src: str) -> str:
    """Tag single-line top-level `import x` statements so ruff leaves them alone.

    Importing a module can have side effects, so only `from x import y` names
    are ever removed. The tag is stripped again right after ruff runs.
    """
    tree = safety._parse(src)
    if tree is None or KEEP_MARK in src:
        return src
    lines = src.split("\n")
    for node in tree.body:
        if isinstance(node, ast.Import) and node.lineno == node.end_lineno:
            i = node.lineno - 1
            if "#" not in lines[i] and ";" not in lines[i]:
                lines[i] = lines[i].rstrip("\r") + KEEP_MARK + ("\r" if lines[i].endswith("\r") else "")
    return "\n".join(lines)


def step_unused_imports(repo: Path, files: list[Path], cfg: dict, notes: list[str]) -> None:
    if not cfg["python"]["remove_unused_imports"]:
        return
    targets = [f for f in files if f.suffix == ".py" and f.name != "__init__.py"]
    if not targets:
        return
    originals = {f: read(f) for f in targets}
    for f, old in originals.items():
        write(f, _protect_bare_imports(old))
    sh(["ruff", "check", "--select", "F401", "--fix", "--no-cache", "--exit-zero", "--quiet",
        *map(str, targets)], cwd=repo, check=False)
    index = None
    for f, old in originals.items():
        new = read(f).replace(KEEP_MARK, "")
        write(f, new)
        if new == old:
            continue
        ok, why = safety.only_unused_from_imports_removed(old, new)
        if ok:
            index = index or PyIndex(repo, cfg)
            why = index.reexport_risk(f, safety.removed_import_names(old, new), old)
            ok = not why
        if not ok:
            write(f, old)
            notes.append(f"`{f.relative_to(repo)}`: left imports alone ({why})")


def pick_python_formatter(repo: Path, cfg: dict) -> str | None:
    choice = cfg["python"]["formatter"]
    if choice in ("black", "ruff"):
        return choice
    if choice == "off":
        return None
    py = repo / "pyproject.toml"
    text = read(py) if py.is_file() else ""
    if "[tool.black" in text:
        return "black"
    if "[tool.ruff" in text or (repo / "ruff.toml").is_file() or (repo / ".ruff.toml").is_file():
        return "ruff"
    return "black"


def step_python_format(repo: Path, files: list[Path], cfg: dict, notes: list[str]) -> str | None:
    targets = [f for f in files if f.suffix == ".py"]
    tool = pick_python_formatter(repo, cfg)
    if not targets or not tool:
        return None
    originals = {f: read(f) for f in targets}
    if tool == "black":
        sh(["black", "--quiet", *map(str, targets)], cwd=repo, check=False)
    else:
        sh(["ruff", "format", "--no-cache", "--quiet", *map(str, targets)], cwd=repo, check=False)
    for f, old in originals.items():
        new = read(f)
        if new != old and not safety.same_behaviour(old, new):
            write(f, old)
            notes.append(f"`{f.relative_to(repo)}`: formatting reverted (syntax tree changed)")
    return tool


def step_prettier(repo: Path, files: list[Path], cfg: dict, notes: list[str]) -> bool:
    if not cfg["prettier"]["enabled"] or not shutil.which("npx"):
        return False
    exts = set(cfg["prettier"]["extensions"])
    paths = [f for f in files if f.suffix in exts]
    targets = [str(f) for f in paths]
    # Plain JS files must still parse afterwards; anything that doesn't is put back.
    js = {f: read(f) for f in paths if f.suffix in (".js", ".mjs", ".cjs")}
    parsed_before = {f for f in js if sh(["node", "--check", str(f)], check=False).returncode == 0}
    for i in range(0, len(targets), 200):
        r = sh(["npx", "--yes", PRETTIER, "--write", "--log-level", "warn", "--ignore-unknown",
                *targets[i:i + 200]], cwd=repo, check=False)
        if "npm error" in (r.stderr or ""):
            notes.append("prettier could not be downloaded; JS/TS/CSS formatting skipped")
            break
        if r.returncode not in (0, 2):
            notes.append("prettier reported errors on some files; those were left unchanged")
    for f in parsed_before:
        if read(f) != js[f] and sh(["node", "--check", str(f)], check=False).returncode != 0:
            write(f, js[f])
            notes.append(f"`{f.relative_to(repo)}`: formatting reverted (no longer parsed)")
    return bool(targets)


def clean_checkout(repo: Path, files: list[Path], cfg: dict, author: tuple[str, str]):
    """Run every cleanup step on `repo`, one commit per step. Returns (commits, notes)."""
    commits: list[str] = []
    notes: list[str] = []

    step_unused_imports(repo, files, cfg, notes)
    if commit(repo, "refactor: remove unused imports", author):
        commits.append("Removed unused `from … import …` names (checked: nothing else in the file changed, and no other file imports them)")

    tool = step_python_format(repo, files, cfg, notes)
    if tool and commit(repo, f"style: format Python with {tool}", author):
        commits.append(f"Formatted Python with {tool} (checked: syntax tree identical before and after)")

    if step_prettier(repo, files, cfg, notes) and commit(repo, "style: format web code with prettier", author):
        commits.append("Formatted JS/TS/CSS with prettier (layout only)")

    return commits, notes


# ---------------------------------------------------------------- per repo

@dataclass
class Result:
    repo: str
    status: str  # merged | review | open | dry-run | clean | skipped | failed
    detail: str = ""
    url: str = ""
    patch: str = ""


@dataclass
class Ctx:
    cfg: dict
    token: str
    author: tuple[str, str]
    state: dict
    dry_run: bool
    stamp: str
    workdir: Path
    results: list[Result] = field(default_factory=list)


def ensure_labels(full: str, token: str) -> None:
    gh(["label", "create", LABEL, "-R", full, "--color", "C5DEF5", "--force",
        "--description", "Automatic code cleanup"], token, check=False)
    gh(["label", "create", REVIEW_LABEL, "-R", full, "--color", "FBCA04", "--force",
        "--description", "Janitor change that is too big to merge automatically"], token, check=False)


def pr_body(commits, notes, scope, lines, nfiles, small, auto_merge) -> str:
    out = ["Automatic cleanup by repo-janitor. Layout and unused imports only; no logic changes.", "",
           f"**Scope:** {scope}", f"**Size:** {lines} changed lines in {nfiles} files", ""]
    out += [f"- {c}" for c in commits]
    if notes:
        out += ["", "**Skipped on purpose:**"] + [f"- {n}" for n in notes[:30]]
    out += ["", ("Small enough to merge automatically." if small and auto_merge
                 else "Too big to merge automatically, so it's waiting for your review.")]
    return "\n".join(out)


def process_repo(full: str, ctx: Ctx) -> Result:
    cfg, token = ctx.cfg, ctx.token
    info = json.loads(gh(["api", f"repos/{full}"], token).stdout)
    if info.get("archived"):
        return Result(full, "skipped", "archived")
    if info.get("fork"):
        return Result(full, "skipped", "fork")
    default = info["default_branch"]

    open_prs = json.loads(gh(["pr", "list", "-R", full, "--label", LABEL, "--state", "open",
                              "--json", "url"], token).stdout)
    if open_prs:
        return Result(full, "skipped", "earlier cleanup PR still open", open_prs[0]["url"])

    repo = ctx.workdir / full.replace("/", "__")
    base = os.environ.get("JANITOR_GIT_BASE")  # only used by tests
    url = f"{base}/{full}.git" if base else f"https://x-access-token:{token}@github.com/{full}.git"
    sh(["git", "clone", "--quiet", "--branch", default, url, str(repo)])
    head = sh(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    last = ctx.state.get(full)
    if last == head:
        return Result(full, "skipped", "no new commits")

    if last and sh(["git", "cat-file", "-e", f"{last}^{{commit}}"], cwd=repo, check=False).returncode == 0:
        rels = git_list(repo, ["diff", "--name-only", "--diff-filter=AMR", last, head])
        scope = f"files changed since `{last[:7]}`"
    else:
        rels = git_list(repo, ["ls-files"])
        scope = "whole repository (first run)"
    files = filter_files(repo, rels, cfg)
    if not files:
        if not ctx.dry_run:
            ctx.state[full] = head
        return Result(full, "skipped", "no code files to check")

    branch = f"janitor/{ctx.stamp}"
    sh(["git", "checkout", "-q", "-b", branch], cwd=repo)
    commits, notes = clean_checkout(repo, files, cfg, ctx.author)
    if not commits:
        if not ctx.dry_run:
            ctx.state[full] = head
        return Result(full, "clean", "already tidy")

    numstat = git_list(repo, ["diff", "--numstat", f"{head}..{branch}"])
    lines = nfiles = 0
    for row in "\n".join(numstat).splitlines():
        a, d, *_ = row.split("\t")
        lines += int(a) if a.isdigit() else 0
        lines += int(d) if d.isdigit() else 0
        nfiles += 1
    small = lines <= cfg["small_max_lines"] and nfiles <= cfg["small_max_files"]
    size = f"{lines} lines / {nfiles} files"

    if ctx.dry_run:
        OUT_DIR.mkdir(exist_ok=True)
        patch = OUT_DIR / f"{full.replace('/', '__')}.patch"
        patch.write_text(sh(["git", "diff", f"{head}..{branch}"], cwd=repo).stdout)
        kind = "small: would auto-merge" if small and cfg["auto_merge_small"] else "big: would need review"
        return Result(full, "dry-run", f"{size} ({kind})", patch=str(patch))

    sh(["git", "push", "-q", "origin", branch], cwd=repo)
    ensure_labels(full, token)
    title = f"Janitor: tidy code ({ctx.stamp[:10]})"
    body = pr_body(commits, notes, scope, lines, nfiles, small, cfg["auto_merge_small"])
    labels = ["--label", LABEL] + ([] if small and cfg["auto_merge_small"] else ["--label", REVIEW_LABEL])
    pr_url = gh(["pr", "create", "-R", full, "--base", default, "--head", branch,
                 "--title", title, "--body", body, *labels], token, cwd=repo).stdout.strip().splitlines()[-1]

    if small and cfg["auto_merge_small"]:
        r = gh(["pr", "merge", pr_url, "--squash", "--delete-branch", "--subject", title,
                "--body", "Automatic cleanup by repo-janitor.\n\nJanitor-Run: yes"], token, cwd=repo, check=False)
        if r.returncode == 0:
            new_head = gh(["api", f"repos/{full}/commits/{default}", "--jq", ".sha"], token).stdout.strip()
            ctx.state[full] = new_head or head
            return Result(full, "merged", size, pr_url)
        ctx.state[full] = head
        return Result(full, "open", f"{size}; could not auto-merge: {redact((r.stderr or '').strip()[-200:])}", pr_url)

    ctx.state[full] = head
    return Result(full, "review", size, pr_url)


# ---------------------------------------------------------------- reporting

ICON = {"review": "🟡", "open": "🟠", "failed": "🔴", "merged": "🟢", "clean": "⚪",
        "skipped": "⚪", "dry-run": "🔵"}
ORDER = ["review", "open", "failed", "merged", "dry-run", "clean", "skipped"]


def render(results: list[Result], dry_run: bool, stamp: str) -> str:
    rows = sorted(results, key=lambda r: ORDER.index(r.status))
    out = [f"## Janitor run {stamp}{' (dry run)' if dry_run else ''}", "",
           "| | Repo | Result | Details |", "|---|---|---|---|"]
    for r in rows:
        link = f"[PR]({r.url}) " if r.url else ""
        out.append(f"| {ICON[r.status]} | {r.repo} | {r.status} | {link}{r.detail.replace('|', '/')} |")
    return "\n".join(out)


def notify(results: list[Result], md: str, stamp: str) -> None:
    """Open an issue in the janitor repo (as github-actions) so you get a notification."""
    need = [r for r in results if r.status in ("review", "open", "failed")]
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not need or not repo or not token:
        return
    title = f"Janitor {stamp[:10]}: {len(need)} item(s) need you"
    gh(["issue", "create", "-R", repo, "--title", title, "--body", md], token, check=False)


# ---------------------------------------------------------------- entry point

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="janitor", description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   default=os.environ.get("DRY_RUN", "").lower() == "true",
                   help="make the changes locally and save patches to out/, but push nothing")
    p.add_argument("--only", default=os.environ.get("ONLY", ""),
                   help="comma-separated repo names to limit this run to")
    p.add_argument("--local", metavar="PATH",
                   help="clean an existing local checkout in place (commits on its current branch, no GitHub)")
    args = p.parse_args(argv)
    cfg = load_config()

    if args.local:
        repo = Path(args.local).resolve()
        files = filter_files(repo, git_list(repo, ["ls-files"]), cfg)
        commits, notes = clean_checkout(repo, files, cfg, ("repo-janitor", "janitor@localhost"))
        print("\n".join(["Commits:"] + [f"  - {c}" for c in commits] + ["Notes:"] + [f"  - {n}" for n in notes]))
        return 0

    token = os.environ.get("JANITOR_TOKEN")
    if not token:
        print("JANITOR_TOKEN is not set (see README.md)", file=sys.stderr)
        return 2

    me = json.loads(gh(["api", "user"], token).stdout)
    author = (cfg["author_name"] or me["login"],
              cfg["author_email"] or f"{me['id']}+{me['login']}@users.noreply.github.com")
    owner = cfg["owner"] or me["login"]
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    repos = [r if "/" in r else f"{owner}/{r}" for r in cfg["repos"]]
    if only:
        repos = [r for r in repos if r.split("/")[1] in only or r in only]

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.is_file() else {}
    stamp = dt.datetime.now(ZoneInfo(cfg["timezone"])).strftime("%Y-%m-%d-%H%M")

    with tempfile.TemporaryDirectory(prefix="janitor-") as tmp:
        ctx = Ctx(cfg, token, author, state, args.dry_run, stamp, Path(tmp))
        for full in repos:
            print(f"→ {full}", flush=True)
            try:
                res = process_repo(full, ctx)
            except Exception as e:  # one bad repo must not stop the others
                res = Result(full, "failed", redact(str(e))[:300])
            print(f"  {res.status}: {res.detail} {res.url}", flush=True)
            ctx.results.append(res)

    md = render(ctx.results, args.dry_run, stamp)
    print("\n" + md)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as fh:
            fh.write(md + "\n")
    if not args.dry_run:
        STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        notify(ctx.results, md, stamp)
    return 0
