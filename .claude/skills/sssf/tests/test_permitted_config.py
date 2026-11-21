"""#54059 AC3: permitted() against the stock template config.

YAML-presence alone (test_templates_present) does not prove the live roster
bars the unrestricted builder from the grader paths. Call permitted() on the
parsed template so a template/permissions drift goes red.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from adw_modules import permissions
from conftest import agent, config

SKILL = Path(__file__).resolve().parents[1]
TEMPLATE_CFG = SKILL / "templates" / "sssf.config.yaml"

# Paths the stock template must bar an unrestricted builder from touching.
GRADER_DENY = (
    "justfile",
    "adws/tests/test_permissions.py",
    "adws/tests/",
    "scripts/run-items-batch.js",
    "scripts/",
    "adws/adw_modules/gates.py",
    "adws/adw_modules/",
    "adws/adw_sssf_config/roster.yaml",
    "adws/adw_build.py",
)

# Ordinary product paths an unrestricted builder may still edit.
# New (untracked) tests/ files stay allowed; existing tracked ones are barred
# via protected_tracked (tested separately).
GRADER_ALLOW = (
    "src/feature.js",
    "tests/new-feature.test.js",
    "package.json",
    "README.md",
)


def _stock_cfg() -> object:
    live = yaml.safe_load(TEMPLATE_CFG.read_text(encoding="utf-8"))
    protected = list(live["defaults"]["protected_files"])
    protected_tracked = list(live["defaults"].get("protected_tracked") or [])
    data_dir = live["defaults"].get("data_dir", "adws/adw_data")
    return config(protected=protected, protected_tracked=protected_tracked,
                  data_dir=data_dir)


def test_stock_template_permitted_denies_grader_paths():
    """MUTATION BAR: drop a grader path from protected_files → this goes red."""
    cfg = _stock_cfg()
    builder = agent("builder", writes=None)
    for path in GRADER_DENY:
        assert not permissions.permitted(path, builder, cfg, frozenset()), (
            f"unrestricted builder must be barred from {path!r} under stock template"
        )


def test_stock_template_permitted_allows_ordinary_product_paths():
    cfg = _stock_cfg()
    builder = agent("builder", writes=None)
    for path in GRADER_ALLOW:
        assert permissions.permitted(path, builder, cfg, frozenset()), (
            f"unrestricted builder must still edit ordinary path {path!r}"
        )


def test_stock_template_permitted_rejects_disallowed_writes_list():
    """A whitelist that omits protected paths cannot open them; naming one can."""
    cfg = _stock_cfg()
    scout = agent("scout", writes=[])
    assert not permissions.permitted("justfile", scout, cfg, frozenset())
    unlocked = agent("maintainer", writes=["justfile"])
    assert permissions.permitted("justfile", unlocked, cfg, frozenset())


def test_stock_template_protected_files_nonempty():
    cfg = _stock_cfg()
    assert cfg.defaults.protected_files, "stock template must declare protected_files"
    assert "tests/" in cfg.defaults.protected_tracked, (
        "stock template must protect existing product tests/ (#54059 AC1)"
    )


def test_stock_template_protected_tracked_bars_existing_product_tests():
    """Existing tracked tests/ files are read-only; brand-new ones stay writable."""
    cfg = _stock_cfg()
    builder = agent("builder", writes=None)
    tracked = frozenset({"tests/old.test.js", "src/feature.js"})
    assert not permissions.permitted("tests/old.test.js", builder, cfg, tracked)
    assert permissions.permitted("tests/brand-new.test.js", builder, cfg, tracked)
    assert permissions.permitted("src/feature.js", builder, cfg, tracked)


def test_unlock_overrides_protected_without_whitelist_flip():
    """#54059 AC2: unlock lets an unrestricted agent touch one protected path
    without flipping writes:null into whitelist-only."""
    cfg = _stock_cfg()
    builder = agent("builder", writes=None, unlock=["scripts/run-items-batch.js"])
    assert permissions.permitted(
        "scripts/run-items-batch.js", builder, cfg, frozenset())
    # Still unrestricted on ordinary product paths:
    assert permissions.permitted("src/feature.js", builder, cfg, frozenset())
    # Other protected paths stay barred:
    assert not permissions.permitted("justfile", builder, cfg, frozenset())
    # Contrast: a writes: list would unlock scripts/ but bar src/:
    whitelisted = agent("builder", writes=["scripts/run-items-batch.js"])
    assert permissions.permitted(
        "scripts/run-items-batch.js", whitelisted, cfg, frozenset())
    assert not permissions.permitted("src/feature.js", whitelisted, cfg, frozenset())
