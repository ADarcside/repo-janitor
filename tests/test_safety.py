import subprocess
import textwrap
from pathlib import Path

from janitor import safety
from janitor.run import clean_checkout, filter_files, git_list, load_config


def d(s):
    return textwrap.dedent(s).lstrip()


# ---------- same_behaviour

def test_formatting_only_is_accepted():
    assert safety.same_behaviour("x=[1,2 ,3]\ndef f( a ):return a*2\n",
                                 "x = [1, 2, 3]\n\n\ndef f(a):\n    return a * 2\n")


def test_value_change_is_rejected():
    assert not safety.same_behaviour("x = 1\n", "x = 2\n")


def test_docstring_reindent_is_accepted_but_rewording_is_not():
    old = 'def f():\n    """Hi.\n       there   \n    """\n'
    assert safety.same_behaviour(old, 'def f():\n    """Hi.\n    there\n    """\n')
    assert not safety.same_behaviour(old, 'def f():\n    """Hello."""\n')


def test_unparseable_is_rejected():
    assert not safety.same_behaviour("print 'py2'\n", "print('py2')\n")


# ---------- only_unused_from_imports_removed

def test_removing_unused_from_import_is_accepted():
    old = "from os import path, sep\nprint(sep)\n"
    new = "from os import sep\nprint(sep)\n"
    assert safety.only_unused_from_imports_removed(old, new) == (True, "")
    assert safety.removed_import_names(old, new) == ["path"]


def test_removing_bare_import_is_rejected():
    ok, why = safety.only_unused_from_imports_removed("import readline\nx = 1\n", "x = 1\n")
    assert not ok and "side effects" in why


def test_removing_import_plus_code_change_is_rejected():
    ok, _ = safety.only_unused_from_imports_removed("from os import path\nx = 1\n", "x = 2\n")
    assert not ok


def test_adding_import_is_rejected():
    ok, _ = safety.only_unused_from_imports_removed("x = 1\n", "from os import path\nx = 1\n")
    assert not ok


def test_nested_import_removal_is_rejected():
    old = "def f():\n    from os import path\n    return 1\n"
    new = "def f():\n    return 1\n"
    assert not safety.only_unused_from_imports_removed(old, new)[0]


# ---------- end to end on a throwaway repo

def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_end_to_end(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "helpers.py").write_text(d("""
        from typing import List, Dict
        import readline
        def total( xs:List[int] ):
            return sum( xs )
        """))
    (repo / "config.py").write_text("from helpers import total\nfrom os import sep\nSETTING=1\n")
    (repo / "main.py").write_text("from config import total\nprint(total([1,2]))\n")
    (repo / "vendor").mkdir()
    (repo / "vendor" / "lib.py").write_text("x=1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")

    cfg = load_config()
    cfg["prettier"]["enabled"] = False
    files = filter_files(repo, git_list(repo, ["ls-files"]), cfg)
    assert "vendor/lib.py" not in {str(f.relative_to(repo)) for f in files}

    commits, notes = clean_checkout(repo, files, cfg, ("t", "t@t"))

    helpers = (repo / "helpers.py").read_text()
    config = (repo / "config.py").read_text()
    assert "Dict" not in helpers            # unused from-import removed
    assert "import readline" in helpers     # bare import kept (side effects)
    assert "def total(xs: List[int]):" in helpers  # formatted
    assert "from helpers import total" in config    # re-exported to main.py: kept
    assert (repo / "vendor" / "lib.py").read_text() == "x=1\n"  # excluded folder untouched
    assert any("left imports alone" in n and "config.py" in n for n in notes)
    assert len(commits) == 2
    # main.py still runs and prints the same thing
    out = subprocess.run(["python", "main.py"], cwd=repo, capture_output=True, text=True)
    assert out.stdout.strip() == "3"
