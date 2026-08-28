#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Plan Build Test — the full starter chain.

Usage:
    uv run adws/adw_plan_build_test.py "<prompt or path/to/prompt.md>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]

Phases: engineer(request) -> planner -> builder -> code(test) [-> builder(fix) -> code(test) ... bounded] -> git(commit)

Testing is CODE: the suite's command lives in adw_modules/quality.py, so no
agent spends a context window rediscovering it. Failures flow back to the
builder as an envelope, and only an exhausted fix loop fails the run.
"""

import argparse
import sys

from adw_modules import agents, gates, git_helper, quality, session, utils
from adw_modules.data_types import AgentCall, BuildOutput, PhaseParams, PlanOutput

REQUIRED_AGENTS = ["planner", "builder"]
MAX_FIX_LOOPS = 3


def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, adw_id)

    def record(ph, result) -> None:
        passed = sum(1 for check in result.checks if check.passed)
        ph.log(passed=result.passed, checks=f"{passed}/{len(result.checks)}",
               artifacts=", ".join(result.artifacts))

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input=prompt)

    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                               description="Turn the request into an implementable plan")) as ph:
        plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                                 gates=[gates.artifacts_exist, gates.files_non_empty]))

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder", retries=1,
                               description="Implement the plan exactly")) as ph:
        # Three gates, not one. Live false-done on 2026-08-23 (adw_id 615f4542):
        # the builder invented src/pull-video.js instead of editing the real
        # pull-video.js at the repo root, and named its test file
        # tests/test_paragraphs.js, which this repo's runner glob never collects.
        # artifacts_exist alone reported "0 checked" and the run committed a
        # feature that did not exist with tests that could not run.
        # Run 2 (f501b92a) then defeated the first patch: diff_matches_claims
        # only asks whether a claimed path EXISTS, so "I changed pull-video.js"
        # passed while the commit touched it zero times; and the first
        # discoverability gate defaulted to "not applicable", so a test written
        # to `ests/` (planner dropped a letter) sailed through.
        #   claims_are_actually_modified -> git must SEE the change, not just the file
        #   new_tests_are_discoverable   -> fail-closed placement check
        # Now gates.BUILDER_GATES: this list used to be spelled out here and
        # only here, which is how the other three committing ADWs went a month
        # on the single gate these two exist to backstop.
        previous = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=plan,
                                     gates=gates.BUILDER_GATES))

    test = None
    for i in range(1, MAX_FIX_LOOPS + 1):
        with run.phase(PhaseParams(name=f"test_{i}", kind="code", owner="quality",
                                   description="Run the suite — a known command, so code runs "
                                               "it and no agent has to rediscover it")) as ph:
            test = quality.run_tests(run)
            record(ph, test)

        if test.passed:
            break

        with run.phase(PhaseParams(name=f"fix_{i}", kind="agent", owner="builder", retries=1,
                                   description="Repair what the suite reported, from its "
                                               "verbatim output")) as ph:
            previous = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt,
                                         previous=quality.as_envelope(test, "tests"),
                                         gates=gates.BUILDER_GATES))

    # Only tested work gets committed — a red suite leaves the tree uncommitted.
    if test is not None and test.passed:
        with run.phase(PhaseParams(name="commit", kind="code", owner="git",
                                   description="Land the code only after the suite came back green")) as ph:
            message = previous.commit_message or f"sssf({run.adw_id}): {previous.summary}"
            # Stage ONLY what the agents were observed to write, not `git add -A`.
            # See git_helper.commit_paths and the f501b92a post-mortem.
            touched = getattr(run, "agent_touched_paths", [])
            ph.log(sha=git_helper.commit_paths(message, touched),
                   message=message, staged=touched)
            run.agent_touched_paths = []      # drain: these are landed now

    return run.finish(accepted=test is not None and test.passed,
                      reason=f"the suite still failed after {MAX_FIX_LOOPS} fix attempt(s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="inline text or a path to a prompt file")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), args.config, args.adw_id))
