"""Find and run a repo's own test suite, so cleanups are checked against it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

INSTALL_TIMEOUT = 15 * 60
TEST_TIMEOUT = 15 * 60
NPM_PLACEHOLDER = "no test specified"


@dataclass
class Outcome:
    kind: str  # pass | fail | unavailable | none
    detail: str = ""


def _run(cmd, cwd, env=None, timeout=TEST_TIMEOUT):
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout // 60} min"
    except FileNotFoundError as e:
        return 127, str(e)


def _tail(text: str, n: int = 3) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return " / ".join(lines[-n:])[:300]


class RepoTests:
    def __init__(self, repo: Path, env_dir: Path, tracked: list[str]):
        self.repo = repo
        self.env_dir = env_dir
        self.python = self._has_python_tests(tracked)
        self.node = self._has_node_tests()
        self.venv_python = env_dir / "venv" / "bin" / "python"

    @property
    def found(self) -> bool:
        return self.python or self.node

    def describe(self) -> str:
        return " + ".join(k for k, on in (("pytest", self.python), ("npm test", self.node)) if on)

    @staticmethod
    def _has_python_tests(tracked: list[str]) -> bool:
        for rel in tracked:
            if not rel.endswith(".py"):
                continue
            name = rel.rsplit("/", 1)[-1]
            parts = rel.split("/")
            if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
                return True
            if any(p in ("tests", "test") for p in parts[:-1]):
                return True
        return False

    def _has_node_tests(self) -> bool:
        pkg = self.repo / "package.json"
        if not pkg.is_file():
            return False
        try:
            test = (json.loads(pkg.read_text()).get("scripts") or {}).get("test", "")
        except (ValueError, AttributeError):
            return False
        return bool(test) and NPM_PLACEHOLDER not in test

    # ------------------------------------------------------------ install

    def install(self) -> str:
        """Install dependencies. Returns an error message, or "" on success."""
        if self.python:
            err = self._install_python()
            if err:
                return f"Python dependencies didn't install ({err})"
        if self.node:
            lock = (self.repo / "package-lock.json").is_file()
            cmd = ["npm", "ci", "--no-audit", "--no-fund"] if lock else \
                  ["npm", "install", "--no-audit", "--no-fund"]
            code, out = _run(cmd, self.repo, timeout=INSTALL_TIMEOUT)
            if code != 0:
                return f"npm dependencies didn't install ({_tail(out, 2)})"
        return ""

    def _install_python(self) -> str:
        cmd = [sys.executable, "-m", "venv", str(self.env_dir / "venv")]
        if os.environ.get("JANITOR_VENV_SYSTEM_SITE") == "1":  # offline test runs only
            cmd.append("--system-site-packages")
        code, out = _run(cmd, self.repo)
        if code != 0:
            return _tail(out, 1)
        pip = [str(self.venv_python), "-m", "pip", "install", "-q", "--disable-pip-version-check"]
        steps = []
        pyproject = self.repo / "pyproject.toml"
        if pyproject.is_file() or (self.repo / "setup.py").is_file() or (self.repo / "setup.cfg").is_file():
            extras = []
            if pyproject.is_file():
                try:
                    data = tomllib.loads(pyproject.read_text())
                    opt = (data.get("project") or {}).get("optional-dependencies") or {}
                    extras = [e for e in opt if e in ("test", "tests", "testing", "dev")]
                    packaged = "project" in data or "build-system" in data
                except (tomllib.TOMLDecodeError, UnicodeDecodeError):
                    packaged = False
            else:
                packaged = True
            if packaged:
                steps.append(pip + ["-e", f".[{','.join(extras)}]" if extras else "."])
        for name in ("requirements.txt", "requirements-dev.txt", "requirements_dev.txt",
                     "dev-requirements.txt", "requirements-test.txt", "test-requirements.txt"):
            if (self.repo / name).is_file():
                steps.append(pip + ["-r", name])
        steps.append(pip + ["pytest"])
        for cmd in steps:
            code, out = _run(cmd, self.repo, timeout=INSTALL_TIMEOUT)
            if code != 0:
                return _tail(out, 2)
        return ""

    # ------------------------------------------------------------ run

    def run(self) -> Outcome:
        results = []
        env = {**os.environ, "CI": "true"}
        if self.python:
            code, out = _run([str(self.venv_python), "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                             self.repo, env=env)
            if code is None:
                return Outcome("unavailable", f"pytest {out}")
            if code == 5:
                results.append(Outcome("none", "pytest found no tests"))
            elif code == 0:
                results.append(Outcome("pass", _tail(out, 1)))
            else:
                return Outcome("fail", f"pytest: {_tail(out)}")
        if self.node:
            code, out = _run(["npm", "test", "--silent"], self.repo, env=env)
            if code is None:
                return Outcome("unavailable", f"npm test {out}")
            if code != 0:
                return Outcome("fail", f"npm test: {_tail(out)}")
            results.append(Outcome("pass", "npm test passed"))
        if any(r.kind == "pass" for r in results):
            return Outcome("pass", "; ".join(r.detail for r in results))
        return Outcome("none", "no tests were collected")
