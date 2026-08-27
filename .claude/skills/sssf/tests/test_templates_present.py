"""Smoke tests so CI is never collection-empty on main.

#52882 adds behavioral gates tests beside this file. These only assert the
stamp surface exists — a missing templates tree is a red CI, not a skip.
"""
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
MODULES = SKILL / "templates" / "adws" / "adw_modules"


def test_gates_module_exists():
    assert (MODULES / "gates.py").is_file()


def test_git_helper_module_exists():
    assert (MODULES / "git_helper.py").is_file()
