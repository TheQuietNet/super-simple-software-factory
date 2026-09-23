"""Config loading/validation and agent execution.

Every ADW validates its agents before running (fail fast, nothing spawns
against a half-valid config). Every agent call parses against a concrete
output type; parse failures and gate violations re-prompt the SAME session
with a correction — context intact, bounded retries. Agent proposes, code
disposes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import yaml

from . import (agent_budget, agent_cc, agent_grok, agent_pi, gates, isolation,
              loop_guard, permissions, prompts)
from .data_types import (AgentCall, AgentConfig, EnvelopeBase, EventRecord,
                         GateCheck, GateReport, Phase, PiRequest, PiResult,
                         SSSFConfig, UsageBreakdown)
from .utils import new_id

_SUPPORTED_CODING_AGENTS = ("pi", "claude_code", "grok")

JSON_FIX_ATTEMPTS = 2      # continue-with-correction attempts for malformed JSON
# Even with per-signature scoping (a genuinely different loop gets its own
# correction), a phase must not be able to correct forever just by finding
# new ways to loop — this many DISTINCT signatures is the most one phase call
# gets before the next one, same or different, fails it outright.
MAX_LOOP_CORRECTIONS = 3


class GateFailure(RuntimeError):
    pass


_NON_CORRECTABLE_ROLLBACK_OUTCOMES = (
    "could not roll back",
    "could not delete",
    "could not unstage",
    "left as-is",
    "REVERTED-BY-AGENT",
    "moved HEAD",          # also covers a branch-switch-to-same-commit ref movement
    "control plane",
)


def permission_breach_is_correctable(breach: permissions.PermissionBreach) -> bool:
    """One in-session retry is only safe after a successful rollback.

    A failed rollback (`could not roll back` / `could not delete`) leaves the
    unauthorized bytes on disk; re-prompting would write on top of a dirty
    tree.

    A pre-dirty path's two possible outcomes — `left as-is` and
    `REVERTED-BY-AGENT` — are EQUALLY non-correctable, for the same reason and
    a sharper one. `permissions._roll_back` deliberately never touches a
    pre-dirty path's content (see its own docstring), so "left as-is" means
    the agent's unauthorized write — including, critically, anything it
    `git add`-ed — is still sitting there completely un-undone. Re-prompting
    for a "fix" hands it a second turn to revert just the WORKING TREE back
    to baseline while leaving that same edit STAGED in the index; the next
    `permissions.enforce()` call would then see the working tree matching
    baseline again and report no breach, while `git_helper.commit_paths`
    still had a live path to sweep the leftover staged blob into a commit
    that never claimed it. Dying here instead closes that retry window
    entirely, before the second turn can ever run.

    A HEAD-movement breach and a git-control-plane breach are equally
    non-correctable — there is no single "path" to roll back for either, and
    both are exactly the kind of thing a re-prompt cannot be trusted to leave
    alone a second time.
    """
    text = str(breach)
    return not any(marker in text for marker in _NON_CORRECTABLE_ROLLBACK_OUTCOMES)


# ── config ───────────────────────────────────────────────────────────────────

def load_config(path: str = "adws/adw_sssf_config/sssf.config.yaml") -> SSSFConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults", {}) or {}
    for agent in raw.get("agents", []) or []:
        for key in ("coding_agent", "model", "thinking", "color", "tools", "writes",
                    "timeout_seconds"):
            if key in defaults:
                agent.setdefault(key, defaults[key])
        agent.setdefault("harness_engineering", defaults.get("harness_engineering", []))
    return SSSFConfig(**raw)


def resolve(cfg: SSSFConfig, name: str) -> AgentConfig:
    for agent in cfg.agents:
        if agent.name == name:
            return agent
    raise SystemExit(f"agent {name!r} is not defined in the config — "
                     f"available: {[a.name for a in cfg.agents]}")


def validate(cfg: SSSFConfig, required: list[str]) -> None:
    """Fail fast: every required name must resolve to a usable agent."""
    problems = []
    for name in required:
        try:
            agent = resolve(cfg, name)
        except SystemExit as e:
            problems.append(str(e))
            continue
        if agent.coding_agent not in _SUPPORTED_CODING_AGENTS:
            problems.append(
                f"agent {name!r}: coding_agent {agent.coding_agent!r} "
                f"is not supported (want one of {_SUPPORTED_CODING_AGENTS})")
        # timeout_seconds is a plain int on AgentConfig (not a pydantic
        # conint) specifically so an invalid roster value survives
        # construction and lands here, alongside every other config problem,
        # instead of failing load_config() with a bare pydantic
        # ValidationError before the aggregated report below can even run.
        if agent.timeout_seconds <= 0:
            problems.append(
                f"agent {name!r}: timeout_seconds must be a positive int "
                f"(got {agent.timeout_seconds!r})")
        for label, ref in (("system", agent.prompt_engineering.system),
                           ("user", agent.prompt_engineering.user)):
            if not Path(ref).is_file():
                problems.append(f"agent {name!r}: {label} prompt not found: {ref}")
        if agent.coding_agent == "pi":
            try:
                agent_pi.resolve_model(agent.model)
            except ValueError as e:
                problems.append(f"agent {name!r}: {e}")
        elif agent.coding_agent in ("claude_code", "grok"):
            if not (agent.model or "").strip():
                problems.append(f"agent {name!r}: model is empty")
    if problems:
        raise SystemExit("config validation failed:\n- " + "\n- ".join(problems))


# ── execution ────────────────────────────────────────────────────────────────

def execute(run, phase: Phase, call: AgentCall) -> EnvelopeBase:
    """One agent call: render prompts -> pi run -> typed parse -> gates -> envelope."""
    agent = resolve(run.cfg, phase.params.owner)
    agent_dir = run.session_dir / agent.name
    agent_dir.mkdir(parents=True, exist_ok=True)

    # ONE deadline for the WHOLE phase call, pinned here before ANY of this
    # phase's work — not a fresh timeout_seconds handed to every turn. A
    # per-send budget would let a phase legitimately run agent.timeout_seconds
    # MULTIPLE TIMES OVER (initial turn, a JSON retry, a gate correction, a
    # permission correction, a loop correction each getting their own full
    # budget) — the opposite of a budget.
    #
    # This ABSOLUTE value is threaded straight into every PiRequest.deadline,
    # with no flooring anywhere (a `float`, `time.monotonic()`-comparable end
    # to end). Flooring the remaining time to an int and handing THAT as a
    # fresh relative timeout_seconds to the adapter (which then re-derives
    # its own deadline from ITS OWN start time) loses sub-second precision
    # twice over, and means a genuinely-configured short budget (1s) could
    # NEVER start at all: ordinary git/fixture setup overhead before the
    # first send() already eats under a second. The pre-check in send()
    # below only refuses an ALREADY-passed deadline; everything else is left
    # to the adapter's own sub-second-precise enforcement against this same
    # absolute value.
    #
    # Pinned BEFORE isolation.prepare() runs below: prepare()'s own full-tree
    # copy/scan (unbounded: every tracked+untracked file in the repo) must
    # count against the phase's own budget too, and an agent that finished
    # its last send just before expiry must not have sync()/finalize() run
    # an arbitrarily long tree scan/copy afterward while the phase still
    # reports success. Wrapped in `isolation.Budget` so it threads through
    # prepare()/sync()/finalize()/apply_back() via the `Isolation` object
    # those already take — no new parameter needed on any of them.
    phase_deadline = time.monotonic() + agent.timeout_seconds
    budget = isolation.Budget(deadline=phase_deadline, nominal_seconds=agent.timeout_seconds)

    # Every agent phase runs against a disposable COPY of the repo, never the
    # real worktree — see adw_modules/isolation.py's module docstring for the
    # full design. `iso.copy_root` becomes the agent subprocess's cwd below;
    # `context_handoff_dir` is rewritten to the copy's own handoff dir so a
    # plan/review/findings file the agent writes there is harvested back by
    # isolation.sync()/finalize() rather than handed an absolute path into
    # the real repo it can never reach.
    iso = isolation.prepare(run, agent, budget)
    # A builder-class call — one whose output type carries `changed_files`,
    # the exact set `gates.BUILDER_GATES` already applies to — additionally
    # requires apply-back to stay inside the PINNED requested scope, not just
    # `permissions.permitted()`. An unrestricted builder (`writes: None`)
    # passes `permitted()` for almost any ordinary path, so without this an
    # out-of-scope edit would land in the real tree — exactly what isolation
    # exists to prevent — before `claims_are_in_requested_scope`/
    # `scoped_for_commit` ever got a chance to say no. See
    # isolation.apply_back's own docstring for the full rationale.
    require_scope = "changed_files" in call.output_type.model_fields

    variables = {
        "prompt": call.prompt,
        "previous_envelope": call.previous.model_dump_json(indent=2) if call.previous else "(none)",
        "context_handoff_dir": str(iso.copy_root / iso.context_handoff_rel),
    }
    # Gates read the original ask (Where:) off the run, not the envelope.
    run.request = call.prompt
    system_text = prompts.render(agent.prompt_engineering.system, variables)
    user_text = prompts.render(agent.prompt_engineering.user, variables)
    prompts.save(agent_dir / "prompts", "system.md", system_text)
    prompts.save(agent_dir / "prompts", "user.md", user_text)

    session_id = _agent_session_id(run, agent)
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_start", name=agent.name,
                                 payload={"model": agent.model, "thinking": agent.thinking,
                                          "color": agent.color,
                                          "session_id": session_id,
                                          "coding_agent": agent.coding_agent,
                                          "purpose": agent.purpose,
                                          "tools": agent.tools,  # None = all tools
                                          "harness_engineering": agent.harness_engineering}))
    run.console.agent_started(agent.name, agent.model, session_id)

    # Parse retries and gate corrections re-enter the SAME coding-agent session,
    # so the last send is the one whose context occupancy is current — while
    # spend is the opposite: every send costs, so usage accumulates.
    latest: PiResult | None = None
    spent = UsageBreakdown()
    turn = 0  # first turn creates the session; later turns resume it (Claude)
    # phase_deadline/budget are established above, before isolation.prepare().
    # One in-session correction PER DISTINCT loop signature, not one per
    # phase. A phase that hits loop A, gets corrected, and later — genuinely
    # — hits a DIFFERENT loop B must not fail outright on B; B has never been
    # corrected yet. Only the SAME signature/cycle recurring after its own
    # correction fails the phase. Still capped overall: MAX_LOOP_CORRECTIONS
    # distinct signatures is the most this phase gets, so an agent that keeps
    # finding NEW ways to loop cannot correct forever either. Iterative, not
    # recursive: a long correction chain used to recurse `send()` into itself
    # once per correction.
    corrected_signatures: set[str] = set()

    def send(prompt_text: str) -> PiResult:
        nonlocal latest, turn
        while True:
            turn += 1
            if phase_deadline <= time.monotonic():
                # Nothing left AT ALL — this turn (a JSON/gate/permission/
                # loop correction whose earlier turns already spent the
                # whole deadline) is cut off here, before a coding-agent
                # process is even spawned, naming the phase's ORIGINAL
                # total (agent.timeout_seconds is constant across turns —
                # see PiRequest.timeout_seconds's own docstring).
                raise agent_budget.PhaseTimeout(agent.timeout_seconds)
            session_subdir = {
                "claude_code": "claude_sessions",
                "grok": "grok_sessions",
            }.get(agent.coding_agent, "pi_sessions")
            # pi's own session bookkeeping and raw output live under the
            # COPY, never an absolute path into the real repo — an absolute
            # agent_dir path here would be handed straight to the agent
            # subprocess via --session/--session-dir argv, leaking the real
            # root even though cwd is a boundary. Harvested back to the real
            # agent_dir (harness-only, never re-read by the agent) after
            # every turn by isolation.sync(), below.
            copy_agent_dir = iso.copy_root / iso.runtime_rel
            request = PiRequest(
                prompt=prompt_text,
                system_prompt=system_text,
                model=agent.model,
                thinking=agent.thinking,
                session_id=session_id,
                session_dir=str((copy_agent_dir / session_subdir).resolve()),
                raw_output_path=str((copy_agent_dir / "raw_output.jsonl").resolve()),
                tools=agent.tools,
                extensions=agent.harness_engineering,
                # The isolated copy, never run.repo_root — see isolation.py.
                cwd=str(iso.copy_root),
                # PWD/OLDPWD scrubbed of the real repo path too, not just
                # GIT_* (which utils.operator_env() already handles).
                env=isolation.agent_env(run, iso),
                timeout_seconds=agent.timeout_seconds,   # nominal — for messaging only
                deadline=phase_deadline,                 # the real, absolute cutoff
            )
            if agent.coding_agent == "claude_code":
                runner = agent_cc
            elif agent.coding_agent == "grok":
                runner = agent_grok
            else:
                runner = agent_pi
            run_kwargs = dict(
                on_event=_event_forwarder(run, phase, agent.name),
                on_spawn=lambda pid: run.tracer.process_start(
                    run.adw_id, "agent", agent.name, pid,
                    f"{agent.coding_agent} {agent.name} {agent.model}"),
                on_exit=lambda pid: run.tracer.process_end(run.adw_id, pid),
            )
            try:
                if agent.coding_agent in ("claude_code", "grok"):
                    result = runner.run(request, resume=(turn > 1), **run_kwargs)
                else:
                    result = runner.run(request, **run_kwargs)
            except loop_guard.LoopDetected as detected:
                already_corrected = detected.signature in corrected_signatures
                at_cap = len(corrected_signatures) >= MAX_LOOP_CORRECTIONS
                run.tracer.event(EventRecord(
                    adw_id=run.adw_id, phase_id=phase.phase_id,
                    type="error", name="loop_detected",
                    payload={"agent": agent.name, "signature": detected.signature,
                             "corrected": already_corrected, "at_cap": at_cap}))
                if already_corrected or at_cap:
                    # str(detected) is already "agent loop detected: <signature>".
                    raise RuntimeError(str(detected)) from detected
                corrected_signatures.add(detected.signature)
                run.console.retry(agent.name, 1, 1, str(detected))
                prompt_text = (
                    f"You are repeating the same tool call(s): {detected.signature}. "
                    "This is not progress — stop immediately. Review what you have "
                    "already done, take a genuinely different next step, and continue "
                    "toward your Report JSON without repeating this pattern. If the "
                    "work is already done, emit your Report JSON now instead of "
                    "calling any more tools."
                )
                continue   # iterative retry — see the bonus-fix note above
            run.add_usage(result.tokens, result.cost)
            spent.merge(result.usage)
            latest = result
            # Apply whatever the agent wrote THIS turn that is already
            # permitted, and harvest its handoff dir, so gates evaluating
            # run.repo_root right after this call see accurate state. Never
            # raises — a still-unapplied path is only ever a phase-failing
            # breach at finalize(), below, via the same PermissionBreach path
            # permissions.enforce() already used.
            isolation.sync(run, agent, iso, require_scope=require_scope)
            return result

    # Refuse to even START this phase if a submodule is out of sync — see
    # the function's own docstring for why (this harness cannot safely
    # fingerprint uncommitted changes inside a submodule's own working
    # tree). A no-op for a repo with none.
    permissions.assert_no_dirty_submodules(run)

    # What the tree looked like before this agent got its hands on it. Every
    # send in this phase — first prompt, JSON retries, gate corrections — is
    # measured against this one baseline.
    #
    # `phase_base` PINS the current HEAD sha (or the empty-tree sha on an
    # unborn repo) exactly once, here, before the agent's first prompt — the
    # SAME value is threaded through every `snapshot()`/`enforce()` call
    # below, all the way through both permission-breach retries. A literal,
    # live "HEAD" re-resolved on each call would go blind the moment an
    # agent commits during its own phase: the tree looks clean relative to
    # the NEW head, erasing the evidence a HEAD-relative diff would
    # otherwise have shown.
    phase_base = permissions.phase_base(run)
    # The SAME phase-start moment's symbolic ref (branch name, or
    # "DETACHED"). `phase_base` alone is a sha, and a `git switch` to a
    # DIFFERENT branch pointing at that exact same sha would move HEAD
    # without the sha check in `enforce()` ever noticing — this catches
    # that half.
    ref_before = permissions.symbolic_ref_state(run.repo_root)
    tree_before = permissions.snapshot(run, phase_base)
    # The SAME phase-start moment's control-plane fingerprint (.git/config,
    # .git/info/*, .git/hooks/*, index assume-unchanged/skip-worktree
    # flags), diffed against a fresh one inside `enforce()` — deliberately
    # kept off `tree_before`/`run.phase_baseline_dirty` itself; see
    # `permissions.control_plane_snapshot`'s docstring for why.
    meta_before = permissions.control_plane_snapshot(run)
    # gates.claims_are_in_requested_scope runs inside the loop below, BEFORE
    # permissions.enforce() (which derives run.agent_touched_paths) is ever
    # called for this phase — enforce() only runs once, after gates already
    # passed. So a gate that wants "what did THIS phase's agent actually
    # touch" cannot read agent_touched_paths; it has to diff against a
    # baseline. tree_before, taken here, IS that baseline — stashed on the
    # run so the gate can reach it without a new parameter threaded through
    # every gate signature.
    #
    # The FULL fingerprint dict, not just its keys: permissions.snapshot()
    # fingerprints by content (sha256 of the working-tree bytes), not just
    # path presence — carrying only the key set here would make a further
    # edit to an already-dirty, out-of-scope path invisible to the gate
    # below (the path was already in the baseline, so subtracting a bare key
    # set drops it regardless of whether its content changed again during
    # this phase). Copied, not aliased: this dict must stay exactly what it
    # was at THIS moment even though `tree_before` the local variable is
    # never mutated after this point either — the copy just makes that
    # guarantee independent of anyone changing this function later.
    run.phase_baseline_dirty = dict(tree_before)

    result = send(user_text)
    envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # claim gates — violations flow back into the SAME session as corrections
    for gate_attempt in range(1, max(1, phase.params.retries + 1) + 1):
        # JSON retries often re-emit Report with changed_files: [] after the
        # builder already wrote the Where: files. Fill from porcelain ∩ scope
        # BEFORE the gates judge the envelope.
        if gates.fill_empty_changed_files(envelope, run):
            run.tracer.event(EventRecord(
                adw_id=run.adw_id, phase_id=phase.phase_id,
                type="log", name="claims_filled_from_git",
                payload={"attempt": gate_attempt,
                         "changed_files": list(envelope.changed_files)}))
            run.console._emit(
                "  · filled changed_files from git ∩ Where: "
                + ", ".join(envelope.changed_files),
                level="info")
        violations = []
        for gate in call.gates:
            report = _as_report(gate(envelope, run))
            found = report.violations
            run.tracer.gate_row(phase, gate.__name__, report, gate_attempt)
            run.tracer.event(EventRecord(
                adw_id=run.adw_id, phase_id=phase.phase_id,
                type="gate_fail" if found else "gate_pass", name=gate.__name__,
                payload={"attempt": gate_attempt, "violations": found,
                         "checks": [c.model_dump() for c in report.checks]}))
            run.console.gate_result(gate.__name__, report)
            violations.extend(found)
        if not violations:
            break
        if gate_attempt > phase.params.retries:
            raise GateFailure(f"{agent.name} failed gates after {gate_attempt} attempt(s):\n- "
                              + "\n- ".join(violations))
        phase.attempt = gate_attempt
        run.console.retry(agent.name, gate_attempt, phase.params.retries,
                          f"{len(violations)} gate violation(s)")
        correction = ("Your previous response failed validation:\n- "
                      + "\n- ".join(violations)
                      + "\n\nFix these problems, then re-emit ONLY your Report JSON.")
        result = send(correction)
        envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # Permission is checked after every send is done, and before the envelope is
    # accepted: an agent does not get to report success on a phase in which it
    # wrote somewhere it was not allowed to.
    #
    # gates all passed, then the builder touched a protected path. enforce()
    # already rolled the breach back, so the tree is correctable — the old
    # "cannot re-prompt, the write already happened" reason does not hold.
    # One in-session correction; a second breach still dies.
    try:
        # finalize() re-syncs the copy one last time and raises the SAME
        # permissions.PermissionBreach if anything the agent wrote is still
        # un-applied (protected, or outside its writes: list) — this is now
        # the FIRST line of defense, since real writes only ever arrive here
        # through isolation.sync()'s own permitted() filter. enforce() then
        # runs against the now-updated real tree as defense in depth: it
        # should find nothing left to do, but a bug in isolation.py must not
        # silently defeat it.
        isolation.finalize(run, agent, iso, require_scope=require_scope)
        touched = permissions.enforce(run, phase, agent, tree_before, phase_base,
                                      meta_before, ref_before)
    except permissions.PermissionBreach as breach:
        if not permission_breach_is_correctable(breach):
            raise
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="error", name="permission_breach",
                                     payload={"agent": agent.name, "error": str(breach),
                                              "writes": agent.writes,
                                              "protected_files": run.cfg.defaults.protected_files,
                                              "correctable": True}))
        run.console.retry(agent.name, 1, 1, "permission breach rolled back")
        correction = (
            "Unauthorized write did not land in the real repo — either "
            "blocked before being applied, or rolled back. Do not touch "
            "those paths again.\n\n"
            f"{breach}\n\nFix the remaining work without those paths, then "
            "re-emit ONLY your Report JSON."
        )
        result = send(correction)
        envelope, attempt = _parse_with_retries(run, phase, call, result, send)
        if gates.fill_empty_changed_files(envelope, run):
            run.console._emit(
                "  · filled changed_files from git ∩ Where: "
                + ", ".join(envelope.changed_files),
                level="info")
        violations = []
        for gate in call.gates:
            report = _as_report(gate(envelope, run))
            found = report.violations
            run.tracer.gate_row(phase, gate.__name__, report, 1)
            violations.extend(found)
        if violations:
            raise GateFailure(
                f"{agent.name} failed gates after permission correction:\n- "
                + "\n- ".join(violations)) from breach
        try:
            isolation.finalize(run, agent, iso, require_scope=require_scope)   # see the first try block above
            touched = permissions.enforce(run, phase, agent, tree_before, phase_base,
                                          meta_before, ref_before)
        except permissions.PermissionBreach as breach2:
            run.tracer.event(EventRecord(
                adw_id=run.adw_id, phase_id=phase.phase_id,
                type="error", name="permission_breach",
                payload={"agent": agent.name, "error": str(breach2),
                         "writes": agent.writes,
                         "protected_files": run.cfg.defaults.protected_files}))
            raise
    if touched:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="log", name="paths_touched",
                                     payload={"agent": agent.name, "paths": touched}))
        # Accumulate the run's agent-attributed change-set so a later commit
        # phase can stage exactly this and nothing else. `touched` is already
        # the authoritative answer — permissions.enforce derives it by diffing
        # the worktree before and after THIS agent call — it was simply never
        # carried forward, so commit_all fell back to `git add -A` and swept up
        # whatever else happened to be dirty.
        if not hasattr(run, "agent_touched_paths"):
            run.agent_touched_paths = []
        for path in touched:
            if path not in run.agent_touched_paths:
                run.agent_touched_paths.append(path)

    _persist_envelope(run, phase, agent.name, call, envelope, attempt, valid=True)
    run.console.envelope_summary(envelope)
    context = latest or result
    run.tracer.agent_session_row(run.adw_id, agent, session_id,
                                 context_tokens=context.context_tokens,
                                 context_window=context.context_window)
    run.save_agent_map(agent.name, {"session_id": session_id, "model": agent.model,
                                    "coding_agent": agent.coding_agent})
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="handoff", name=agent.name,
                                 payload={"artifacts": envelope.artifacts,
                                          "summary": envelope.summary}))
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_end", name=agent.name,
                                 # Phase totals, not the last send's: a retried
                                 # phase paid for every attempt.
                                 tokens=spent.total_tokens,
                                 payload={"cost": spent.total_cost,
                                          "usage": spent.model_dump(),
                                          "context_tokens": context.context_tokens,
                                          "context_window": context.context_window}))
    run.console.agent_finished(agent.name, spent.total_tokens, spent.total_cost)
    if envelope.status != "success":
        raise RuntimeError(f"{agent.name} reported status={envelope.status!r}: {envelope.summary}")
    return envelope


# ── internals ────────────────────────────────────────────────────────────────

def _as_report(result) -> GateReport:
    """Accept a GateReport, or a legacy gate that returned a violations list."""
    if isinstance(result, GateReport):
        return result
    return GateReport(checks=[GateCheck(item=str(v), ok=False) for v in (result or [])])


def _agent_session_id(run, agent: AgentConfig) -> str:
    entry = run.agent_map.get(agent.name)
    if entry and entry.get("model") == agent.model:
        return entry["session_id"]           # rejoin the existing context window
    return f"sssf-{run.adw_id}-{agent.name}-{new_id(4)}"


def _event_forwarder(run, phase: Phase, agent_name: str):
    """One tool_call event per real tool call, with its exact args and result.

    Also feeds each completed call's (tool, args) into a fresh LoopGuard —
    one instance per turn (agents.execute's `send()` calls this factory
    again on every send, including corrections and retries). N/cycle
    thresholds come from the roster's `defaults`, not the phase, matching
    LoopGuardConfig's own "same pattern, every role" rationale.
    """
    tracker = agent_pi.ToolCallTracker()
    guard = loop_guard.LoopGuard(loop_guard.LoopGuardConfig(
        repeat_count=run.cfg.defaults.loop_repeat_count,
        read_only_repeat_count=run.cfg.defaults.loop_read_only_repeat_count,
        cycle_count=run.cfg.defaults.loop_cycle_count,
    ))

    def forward(event: dict) -> None:
        record = tracker.observe(event)
        if record is None:
            return
        tool, call_args = record["tool"], record["args"]
        # The call's span rides the columns; duration_ms stays in the payload as
        # pi's own authoritative number.
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="tool_call", name=record.pop("label"),
                                     started_at=record.pop("started_at", None),
                                     ended_at=record.pop("ended_at", None),
                                     payload={**record, "agent": agent_name}))
        # Raised AFTER the tracer row above lands, so the triggering call
        # itself is still visible in the trace, not swallowed by the raise.
        guard.observe(tool, call_args)
    return forward


def _extract_json(text: str) -> dict:
    candidate = text
    if "```" in text:
        for block in text.split("```")[1::2]:
            block = block.removeprefix("json").strip()
            if block.startswith("{"):
                candidate = block
                break
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the response")
    return json.loads(candidate[start:end + 1])


def _parse_with_retries(run, phase: Phase, call: AgentCall, result, send):
    """Parse the final response against the declared output type; on failure,
    continue the SAME session with a correction (bounded)."""
    for attempt in range(1, JSON_FIX_ATTEMPTS + 2):
        try:
            payload = _extract_json(result.text)
            return call.output_type.model_validate(payload), attempt
        except Exception as error:
            _persist_envelope(run, phase, phase.params.owner, call, None, attempt,
                              valid=False, raw=result.text)
            if attempt > JSON_FIX_ATTEMPTS:
                raise RuntimeError(
                    f"{phase.params.owner} never produced valid "
                    f"{call.output_type.__name__} JSON: {error}") from error
            run.console.retry(phase.params.owner, attempt, JSON_FIX_ATTEMPTS,
                              f"invalid {call.output_type.__name__} JSON: {error}")
            fields = ", ".join(call.output_type.model_fields.keys())
            result = send(
                f"Your response was not valid JSON for the required structure "
                f"({error}). Respond again with ONLY a JSON object with these "
                f"fields: {fields}. No prose, no code fences.")


def _persist_envelope(run, phase: Phase, agent_name: str, call: AgentCall,
                      envelope: Optional[EnvelopeBase], attempt: int,
                      valid: bool, raw: str = "") -> None:
    payload_json = envelope.model_dump_json(indent=2) if envelope else json.dumps({"raw": raw[-2000:]})
    run.tracer.envelope_row(phase, agent_name, call.output_type.__name__,
                            payload_json, valid, attempt)
    if envelope:
        record = {"agent_name": agent_name, "purpose": resolve(run.cfg, agent_name).purpose,
                  "output_type": call.output_type.__name__, "attempt": attempt,
                  **envelope.model_dump()}
        (run.session_dir / agent_name / "envelope.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8")
