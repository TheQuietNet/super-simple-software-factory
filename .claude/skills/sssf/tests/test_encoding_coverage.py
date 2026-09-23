"""Every text file I/O call in the templates must name its encoding, so a
future call site cannot quietly reintroduce a locale-default crash (Windows'
cp1252 default is the one that actually bit this factory live).

AST walk, not a runtime import — this asks what the code SAYS:
  - every `Path.read_text()` / `Path.write_text()` call, and every
    text-mode `open()`/`Path.open()` call, must carry an `encoding=`
    keyword. A binary-mode open (`"rb"`/`"wb"` etc.) is exempt — passing
    `encoding=` to one is itself an error.
  - every `subprocess.run`/`.Popen`/`.check_output` call with `text=True`
    or `universal_newlines=True` must ALSO carry `encoding=` — decoding a
    child process's stdout/stderr hits the exact same locale default
    otherwise, and several of these capture output that can genuinely
    contain agent- or test-runner-produced unicode.
  - the subprocess check is import-alias-aware: `import subprocess as sp;
    sp.run(...)` and `from subprocess import run as X; X(...)` are caught
    the same as the bare `subprocess.run(...)` form.
  - `.claude/skills/sssf/tests/*.py` is covered too, not just
    `adw_modules/*.py` + `adw_*.py` — the test harness's own subprocess
    calls (fixtures that shell out to git) hit the same locale default as
    production code.
"""

from __future__ import annotations

import ast

import pytest

from conftest import SKILL, TEMPLATES_ADWS

MODULE_FILES = (sorted((TEMPLATES_ADWS / "adw_modules").glob("*.py"))
                + sorted(TEMPLATES_ADWS.glob("adw_*.py"))
                + sorted((SKILL / "tests").glob("*.py")))
_CHECKED_NAMES = ("read_text", "write_text", "open")
_SUBPROCESS_TEXT_CALLS = ("run", "Popen", "check_output")


def _string_mode(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_mode(call: ast.Call) -> str | None:
    """The `mode=`/positional mode argument of an `open()`/`.open()` call."""
    if call.args:
        mode = _string_mode(call.args[0])
        if mode is not None:
            return mode
    for kw in call.keywords:
        if kw.arg == "mode":
            return _string_mode(kw.value)
    return None


def _has_encoding_kwarg(call: ast.Call) -> bool:
    return any(kw.arg == "encoding" for kw in call.keywords)


def _collect_subprocess_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names this file binds to the `subprocess` module, and names it binds
    directly to one of run/Popen/check_output."""
    module_aliases: set[str] = set()
    func_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                for alias in node.names:
                    if alias.name in _SUBPROCESS_TEXT_CALLS:
                        func_aliases.add(alias.asname or alias.name)
    return module_aliases, func_aliases


def _subprocess_call_name(func, module_aliases: set[str], func_aliases: set[str]) -> str | None:
    """The matched run/Popen/check_output name, or None if `func` is not one
    of this file's subprocess call targets (by any alias it bound)."""
    if (isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_TEXT_CALLS
            and isinstance(func.value, ast.Name) and func.value.id in module_aliases):
        return func.attr
    if isinstance(func, ast.Name) and func.id in func_aliases:
        return func.id
    return None


def _is_text_mode(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg in ("text", "universal_newlines"):
            return isinstance(kw.value, ast.Constant) and kw.value.value is True
    return False


def _violations_in_tree(tree: ast.AST, label: str):
    """Shared core: yields one violation string per offending call. `label`
    is just what's printed (a file name, or a fixture description)."""
    module_aliases, func_aliases = _collect_subprocess_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        subprocess_name = _subprocess_call_name(func, module_aliases, func_aliases)
        if subprocess_name is not None:
            if _is_text_mode(node) and not _has_encoding_kwarg(node):
                yield (f"{label}:{node.lineno}: {subprocess_name}(...) "
                      f"is text=True with no encoding= kwarg")
            continue

        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue
        if name not in _CHECKED_NAMES:
            continue
        if name == "open":
            mode = _call_mode(node)
            if mode is not None and "b" in mode:
                continue        # binary mode: no text encoding applies
        if not _has_encoding_kwarg(node):
            yield f"{label}:{node.lineno}: {name}(...) has no encoding= kwarg"


def _violations(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    yield from _violations_in_tree(tree, path.name)


def test_the_module_files_are_actually_discovered():
    """A structural test that silently matches nothing is worse than no test."""
    assert len(MODULE_FILES) >= 10, f"expected adw_modules + adw_*.py, found {MODULE_FILES}"


@pytest.mark.parametrize("path", MODULE_FILES, ids=lambda p: p.name)
def test_every_text_file_io_call_names_its_encoding(path):
    """MUTATION BAR: drop any encoding= kwarg fixed here (or add a new call
    site without one) and this fails, naming the line."""
    violations = list(_violations(path))
    assert not violations, "\n".join(violations)


# ── alias-aware subprocess detection ─────────────────────────────────────────
#
# Fixture SOURCE strings parsed directly (no files written under templates/)
# — the whole point is to prove the AST matcher itself resolves import
# aliases, not to add more real call sites.

def _violations_in_source(source: str) -> list[str]:
    tree = ast.parse(source, filename="<fixture>")
    return list(_violations_in_tree(tree, "<fixture>"))


def test_module_import_alias_with_text_true_and_no_encoding_is_flagged():
    source = (
        "import subprocess as sp\n"
        "def f():\n"
        "    sp.run(['git', 'status'], capture_output=True, text=True)\n"
    )
    violations = _violations_in_source(source)
    assert violations, "sp.run(...) via `import subprocess as sp` must be caught"
    assert "run(...)" in violations[0]


def test_module_import_alias_with_encoding_passes():
    source = (
        "import subprocess as sp\n"
        "def f():\n"
        "    sp.run(['git', 'status'], capture_output=True, text=True,\n"
        "           encoding='utf-8', errors='replace')\n"
    )
    assert _violations_in_source(source) == []


def test_from_import_alias_bare_call_with_universal_newlines_is_flagged():
    source = (
        "from subprocess import run as _run\n"
        "def f():\n"
        "    _run(['git', 'status'], capture_output=True, universal_newlines=True)\n"
    )
    violations = _violations_in_source(source)
    assert violations, "X(...) via `from subprocess import run as X` must be caught"
    assert "_run(...)" in violations[0]


def test_from_import_no_alias_bare_call_with_encoding_passes():
    source = (
        "from subprocess import Popen\n"
        "def f():\n"
        "    Popen(['git', 'log'], stdout=-1, text=True, encoding='utf-8')\n"
    )
    assert _violations_in_source(source) == []


def test_unrelated_same_named_method_is_not_flagged():
    """`widget.run(...)` is not `subprocess.run(...)` just because it is
    also spelled `.run(` — no subprocess import in scope at all here."""
    source = (
        "def f(widget):\n"
        "    return widget.run(text=True)\n"
    )
    assert _violations_in_source(source) == []
