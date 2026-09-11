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


GRADER_PATHS = ("justfile", "adws/tests/", "scripts/")


def test_stock_roster_protects_the_grader():
    """MUTATION BAR: deleting these three lines from the template YAML goes red.

    An unrestricted builder that can rewrite justfile / adws/tests / scripts
    can make the quality gate lie. Same class of hole as the system.md grant.
    """
    import yaml

    cfg_path = SKILL / "templates" / "sssf.config.yaml"
    live = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    protected = live["defaults"]["protected_files"]
    for path in GRADER_PATHS:
        assert path in protected, f"{path} missing from template protected_files"
