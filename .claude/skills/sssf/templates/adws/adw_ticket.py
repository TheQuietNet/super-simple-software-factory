#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""adw_ticket — board -> factory entry point.

Usage:
    uv run adws/adw_ticket.py draft --task 53195
    uv run adws/adw_ticket.py run --task 53195 --from-draft \
        [--chain simple_sdlc|plan_build_test] [--config adws/adw_sssf_config/sssf.config.yaml]

`draft` reads the ticket off the board (via the configurable AGENTCTL
command in `adw_modules/ticket.py` — see SSSF_AGENTCTL_CMD) and writes
`requests/<N>.md` in the four-line ask/Where:/Done means/Out of scope shape
the cookbook (`~/.claude/skills/sssf/cookbooks/how_to_prompt_for_the_eng.md`)
describes — `Where:` built from the ticket's own `footprint`, mapped
repo-relative to THIS repo, never hand-transcribed. Refuses (prints to
stderr, exits 1) when nothing in the footprint maps here.

`run` requires that draft to already exist and be reviewed (`--from-draft`);
without it, it drafts and stops so a human confirms the four lines before any
agent spends a token building against them. Once confirmed: creates
`<worktree_root>/<repo>-<N>-sssf` on `claude/<N>-sssf` off `origin/main`,
claims the ticket (session `<agent>#sssf-<N>`), runs the chosen ADW chain
there heartbeating throughout, and on success pushes + opens a PR citing
`task #N` (never merges — the PR still needs review). On failure it
releases the claim and posts the adw_id/failing phase/violations back to the
ticket. All logic lives in `adw_modules/ticket.py`; this script only parses
argv and prints the result.
"""

from __future__ import annotations

import argparse
import sys

from adw_modules import git_helper, ticket
from adw_modules.ticket import ChainConfig, DraftRefused, RunRefused


def _cmd_draft(args: argparse.Namespace) -> int:
    repo_root = git_helper.repo_root()
    git_helper.assert_cwd_matches_repo(repo_root)
    try:
        result = ticket.draft(args.task, repo_root=repo_root, agent=args.agent,
                              config=args.config)
    except (DraftRefused, ticket.TicketFetchError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"draft written: {result.path}")
    print("---")
    print(result.text, end="")
    print("---")
    if result.dropped:
        print("dropped footprint entries:")
        for note in result.dropped:
            print(f"  - {note}")
    for warning in result.warnings:
        print(f"WARNING: {warning}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    repo_root = git_helper.repo_root()
    git_helper.assert_cwd_matches_repo(repo_root)
    cfg = ChainConfig(task_id=args.task, repo_root=repo_root, config=args.config,
                      chain=args.chain, from_draft=args.from_draft, agent=args.agent)
    try:
        outcome = ticket.run_ticket(cfg)
    except (DraftRefused, RunRefused, ticket.TicketFetchError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    if outcome.stopped_for_review:
        print(outcome.message)
        return 0
    if outcome.ok:
        print(f"shipped: {outcome.pr_url} (adw_id={outcome.adw_id})")
        return 0
    print(f"FAILED: adw_id={outcome.adw_id} phase={outcome.failing_phase}", file=sys.stderr)
    for v in outcome.violations:
        print(f"  - {v}", file=sys.stderr)
    # This used to stop at adw_id/phase/violations, dropping
    # `outcome.message` — the fallback report text `_report_failure` builds,
    # AND any `agentctl msg` delivery error folded into it when posting to
    # the ticket itself failed — leaving both invisible to whoever is
    # watching this run.
    if outcome.message:
        print(outcome.message, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", default=ticket.DEFAULT_AGENT)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_draft = sub.add_parser("draft", help="write requests/<N>.md from the board ticket")
    p_draft.add_argument("--task", type=int, required=True)
    p_draft.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml",
                         help="roster config to check mapped Where: tokens against "
                              "protected_files (best-effort; missing/unreadable skips the check)")
    p_draft.set_defaults(func=_cmd_draft)

    p_run = sub.add_parser("run", help="claim + run the SSSF chain for a drafted ticket")
    p_run.add_argument("--task", type=int, required=True)
    p_run.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    p_run.add_argument("--chain", choices=sorted(ticket.CHAIN_SCRIPTS), default=ticket.DEFAULT_CHAIN)
    p_run.add_argument("--from-draft", action="store_true")
    p_run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
