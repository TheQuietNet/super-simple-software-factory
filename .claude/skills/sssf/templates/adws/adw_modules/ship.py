"""Post-green closeout: commit attributed paths, push, open a PR, route review.

Agent proposes, code ships. After BUILDER_GATES and the suite are green, a
human must not be in the loop for staging, push, or PR body citations.
``git add -A`` is forbidden here — a live incident once swept an operator's
own working-tree overlay into an agent's commit.

``merge --auto`` is invoked last so GitHub squash-merges once CI + review
land. This module does not stamp reviews (an agent cannot stamp its own PR).
"""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import Callable

from . import gates, git_helper

MAIN_NAMES = frozenset({"main", "master"})

# Repo-specific: never hardcode an org/repo here. Set SSSF_SHIP_REPO
# ("owner/repo") once per project, or pass --repo explicitly to adw_ship.py.
# Empty by default — a ship attempt with neither set fails loudly at the
# `gh pr create --repo ""` call rather than silently targeting someone
# else's repo.
DEFAULT_REPO = os.environ.get("SSSF_SHIP_REPO", "").strip()
DEFAULT_REVIEW_ROUTE_REPO = DEFAULT_REPO

# The board-CLI invocation this factory ships against, if any (e.g.
# agentctl or an equivalent ticket-tracker CLI). Configurable via
# SSSF_AGENTCTL_CMD (a shell-quoted command) so the harness never hardcodes
# one operator's path. Falls back to a bare `agentctl` resolved off PATH.
AGENTCTL = shlex.split(os.environ.get("SSSF_AGENTCTL_CMD", "agentctl"))


def pr_body(task_ids: list[int], summary: str = "") -> str:
    """PR body that merge-complete will honour — only the given task ids.

    Naming a neighbour as ``task #N`` in prose (even 'out of scope') can be
    read by an automated ticket-closer as closing it too. This builder
    cannot emit those strings.
    """
    if not task_ids:
        raise ValueError("pr_body refuses an empty task id list")
    cites = " ".join(f"task #{i}" for i in task_ids)
    extra = f"\n\n{summary.strip()}" if (summary or "").strip() else ""
    return f"{cites}{extra}\n"


def pr_title(task_ids: list[int], summary: str) -> str:
    cites = " ".join(f"#{i}" for i in task_ids)
    clip = " ".join((summary or "sssf ship").split())[:72]
    return f"task {cites}: {clip}"


def assert_not_main(branch: str) -> None:
    if (branch or "") in MAIN_NAMES:
        raise RuntimeError(
            f"adw_ship refuses to push {branch!r} — cut a feature branch from "
            "origin/main first")


def commit_green(run, message: str,
                 commit_paths: Callable = git_helper.commit_paths) -> str:
    """Stage ONLY agent_touched_paths, scoped to Where:/plan when one exists, and drain the set.

    This is always the code commit (ship never lands plan/doc output), so
    the same defense in depth as every other commit() applies unconditionally.
    """
    touched = list(getattr(run, "agent_touched_paths", []) or [])
    kept, _excluded = gates.scoped_for_commit(run, touched)
    sha = commit_paths(message, kept)
    run.agent_touched_paths = []
    return sha


def _run(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()}")
    return (result.stdout or "").strip()


def push_head(remote: str = "origin") -> str:
    branch = git_helper.current_branch()
    assert_not_main(branch)
    return _run(["git", "push", "-u", remote, "HEAD"])


def create_pr(*, repo: str, title: str, body: str, head: str) -> str:
    out = _run([
        "gh", "pr", "create",
        "--repo", repo,
        "--base", "main",
        "--head", head,
        "--title", title,
        "--body", body,
    ])
    for line in out.splitlines():
        if line.startswith("http"):
            return line.strip()
    return out


def parse_pr_number(url_or_num: str) -> int:
    text = (url_or_num or "").rstrip("/")
    if text.isdigit():
        return int(text)
    return int(text.rsplit("/", 1)[-1])


def route_reviews(repo: str, *, agentctl: list[str] | None = None) -> str:
    cmd = list(agentctl or AGENTCTL) + [
        "route-reviews", "--repo", repo,
    ]
    return _run(cmd)


def merge_auto(pr: int, repo: str, *, agentctl: list[str] | None = None) -> str:
    cmd = list(agentctl or AGENTCTL) + [
        "merge", "--pr", str(pr), "--repo", repo, "--auto",
    ]
    return _run(cmd)


def ship(run, *, task_ids: list[int], repo: str = DEFAULT_REPO,
         summary: str = "",
         commit_paths: Callable = git_helper.commit_paths,
         pusher: Callable | None = None,
         pr_creator: Callable | None = None,
         reviewer: Callable | None = None,
         merger: Callable | None = None) -> dict:
    """Commit, push, open PR, route reviews, arm auto-merge.

    Callables are injectable so tests never talk to gh or agentctl.
    """
    branch = git_helper.current_branch()
    assert_not_main(branch)
    ids = [int(i) for i in task_ids]
    message = pr_title(ids, summary or getattr(run, "summary", "") or f"sssf({run.adw_id})")
    sha = commit_green(run, f"task #{ids[0]}: {summary or run.adw_id}".strip(),
                       commit_paths=commit_paths)
    (pusher or push_head)()
    body = pr_body(ids, summary)
    url = (pr_creator or (lambda **kw: create_pr(**kw)))(
        repo=repo, title=message, body=body, head=branch)
    pr_num = parse_pr_number(url)
    review_out = (reviewer or (lambda r: route_reviews(r)))(repo)
    merge_out = (merger or (lambda n, r: merge_auto(n, r)))(pr_num, repo)
    return {"sha": sha, "branch": branch, "pr": pr_num, "url": url,
            "review": review_out, "merge": merge_out, "body": body}
