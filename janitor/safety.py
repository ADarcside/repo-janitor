"""Checks that prove a cleanup did not change what Python code does.

Every Python change the janitor makes must pass one of these checks, or the
file is put back exactly as it was.
"""

from __future__ import annotations

import ast
import warnings
from collections import Counter

_DOC_OWNERS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _normalize_doc(text: str) -> str:
    # Formatters may re-indent docstrings or trim trailing spaces in them.
    # Only that whitespace is ignored; any other docstring edit still fails.
    return "\n".join(line.strip() for line in text.strip().splitlines())


class _DocstringNormalizer(ast.NodeTransformer):
    def generic_visit(self, node):
        super().generic_visit(node)
        if isinstance(node, _DOC_OWNERS) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                first.value.value = _normalize_doc(first.value.value)
        return node


def _parse(src: str) -> ast.Module | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return ast.parse(src)
    except (SyntaxError, ValueError):
        return None


def _dump(tree: ast.AST) -> str:
    return ast.dump(_DocstringNormalizer().visit(tree), include_attributes=False)


def same_behaviour(old: str, new: str) -> bool:
    """True when both sources compile to the same syntax tree (formatting-only change)."""
    a, b = _parse(old), _parse(new)
    if a is None or b is None:
        return False
    return _dump(a) == _dump(b)


def _key(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> tuple:
    if isinstance(node, ast.Import):
        return ("import", None, 0, alias.name, alias.asname)
    return ("from", node.module, node.level, alias.name, alias.asname)


def _top_level_imports(tree: ast.Module) -> Counter:
    found: Counter = Counter()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                found[_key(node, alias)] += 1
    return found


def removed_import_names(old: str, new: str) -> list[str]:
    """Names that were bound by top-level imports in `old` but not in `new`."""
    a, b = _parse(old), _parse(new)
    if a is None or b is None:
        return []
    removed = _top_level_imports(a) - _top_level_imports(b)
    return sorted({k[4] or k[3].split(".")[0] for k in removed})


def only_unused_from_imports_removed(old: str, new: str) -> tuple[bool, str]:
    """Accept `new` only if it equals `old` minus some top-level `from x import y` names.

    Bare `import x` statements are never removed because importing a module
    can have side effects (registering plugins, patching, configuring).
    """
    a, b = _parse(old), _parse(new)
    if a is None or b is None:
        return False, "file does not parse"
    before, after = _top_level_imports(a), _top_level_imports(b)
    if after - before:
        return False, "imports were added or changed"
    removed = before - after
    if not removed:
        ok = _dump(a) == _dump(b)
        return ok, "" if ok else "code other than top-level imports changed"
    for kind, module, _level, name, _as in removed:
        if kind == "import":
            return False, f"would remove 'import {name}', which may have side effects"
        if module == "__future__":
            return False, "would remove a __future__ import"

    remaining = removed.copy()
    body = []
    for node in a.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            kept = []
            for alias in node.names:
                k = _key(node, alias)
                if remaining[k] > 0:
                    remaining[k] -= 1
                    continue
                kept.append(alias)
            if not kept:
                continue
            node.names = kept
        body.append(node)
    a.body = body
    ok = _dump(a) == _dump(b)
    return ok, "" if ok else "code other than top-level imports changed"
