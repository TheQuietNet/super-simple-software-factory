#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Plan Build — two-agent chain: planner -> envelope -> builder.

Usage:
    uv run adws/adw_plan_build.py "<prompt or path/to/prompt.md>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]

Phases: engineer(request) -> planner -> builder -> git(commit)
"""

import argparse
import sys

from adw_modules import agents, gates, git_helper, session, utils
from adw_modules.data_types import AgentCall, BuildOutput, PhaseParams, PlanOutput

REQUIRED_AGENTS = ["planner", "builder"]


def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, adw_id)

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input=prompt)

    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                               description="Turn the request into an implementable plan")) as ph:
        plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                                 gates=[gates.artifacts_exist, gates.files_non_empty]))
        # Freeze plan.md's file scope now, while it still reflects only the
        # planner's own gated output — see gates.pin_plan_scope.
        gates.pin_plan_scope(run)

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder", retries=1,
                               description="Implement the plan exactly")) as ph:
        build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=plan,
                                  gates=gates.BUILDER_GATES))

    with run.phase(PhaseParams(name="commit", kind="code", owner="git",
                               description="Land the builder's changes, using the message it wrote")) as ph:
        message = build.commit_message or f"sssf({run.adw_id}): {build.summary}"
        # Only the agents' own change-set — see git_helper.commit_paths.
        touched = getattr(run, "agent_touched_paths", [])
        # Defense in depth — never stage a touched path outside the
        # requested scope, even if it slipped past claims_are_in_requested_scope.
        kept, excluded = gates.scoped_for_commit(run, touched)
        if excluded:
            ph.log(excluded_from_commit=excluded, reason="outside requested scope")
        ph.log(sha=git_helper.commit_paths(message, kept),
               message=message, staged=kept)
        run.agent_touched_paths = []

    return run.finish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="inline text or a path to a prompt file")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), args.config, args.adw_id))
