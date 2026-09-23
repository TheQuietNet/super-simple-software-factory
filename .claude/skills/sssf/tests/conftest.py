"""Shared fixtures for the SSSF template tests.

templates/adws/ is the import root: the stamped ADW scripts run from there
and import `adw_modules.*` directly, so the tests resolve them the same way.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1]
TEMPLATES_ADWS = SKILL / "templates" / "adws"
sys.path.insert(0, str(TEMPLATES_ADWS))

from adw_modules.data_types import (  # noqa: E402
    AgentConfig, ConfigDefaults, PromptEngineering, SSSFConfig,
)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True,
                   encoding="utf-8", errors="replace")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with one committed file.

    Deliberately real rather than a mock — several gates under test read
    `git status --porcelain`/`git diff`, so a fake would only prove the
    parser agrees with whatever the fake emits.
    """
    _git(["init", "-q", "-b", "main"], tmp_path)
    _git(["config", "user.email", "test@example.invalid"], tmp_path)
    _git(["config", "user.name", "adw tests"], tmp_path)
    (tmp_path / "seed.js").write_text("// original\n", encoding="utf-8")
    _git(["add", "seed.js"], tmp_path)
    _git(["commit", "-qm", "seed"], tmp_path)
    return tmp_path


class FakeRun:
    """The attributes gates/permissions/isolation actually reach for."""

    def __init__(self, repo_root: Path, cfg: SSSFConfig | None = None,
                 request: str = "", context_handoff_dir: Path | None = None,
                 adw_id: str = "x"):
        self.repo_root = str(repo_root)
        self.cfg = cfg
        self.request = request
        self.adw_id = adw_id
        self.context_handoff_dir = (
            Path(context_handoff_dir) if context_handoff_dir is not None
            else Path(repo_root) / "adws" / "adw_data" / "sessions" / adw_id / "context_handoff"
        )


def agent(name: str, writes=None, timeout_seconds: int = 300) -> AgentConfig:
    return AgentConfig(
        name=name,
        writes=writes,
        timeout_seconds=timeout_seconds,
        prompt_engineering=PromptEngineering(system="s.md", user="u.md"),
    )


def config(protected: list[str] | None = None,
           data_dir: str = "adws/adw_data") -> SSSFConfig:
    return SSSFConfig(
        defaults=ConfigDefaults(
            protected_files=protected if protected is not None else [],
            data_dir=data_dir,
        ),
        agents=[],
    )


def dirty(repo_root: Path, rel: str, content: str = "x\n") -> None:
    """Write `content` to `rel` under `repo_root`, creating parents — a
    convenience for simulating an agent's own edit without a real commit."""
    path = repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
