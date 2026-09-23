"""Session lifecycle: pin-or-create an adw_id, build the Run object.

`ensure(cfg, adw_id)` joins the session if it exists or creates it under
exactly that id (pinned ids for repeatable runs); omitted, a fresh id is
minted and printed so the next ADW can pick it up.
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
from pathlib import Path

from . import git_helper, isolation
from .data_types import SSSFConfig
from .runner import Run
from .tracer import Tracer
from .utils import engineer_name, new_id


def _finalize_when_killed(run: Run) -> None:
    """A killed run still closes its own trace.

    Python's default SIGTERM handling exits without unwinding, so `just kill`
    (or any `kill <pid>`) would leave the session reading `running` forever and
    its process rows open — the trace would claim work is in flight that is
    already dead. Turning the signal into SystemExit both finalizes here and
    lets the phase context manager record the phase as failed on the way out.
    """
    def handler(signum, _frame):
        run.tracer.session_finish(run.adw_id, ok=False)   # also closes process rows
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)


def ensure(cfg: SSSFConfig, adw_id: str | None = None) -> Run:
    # #53181 reviewer follow-up: Tracer() below is the FIRST filesystem/db side
    # effect an ADW makes — it ensure_dir()s cfg.observability.db's parent and
    # creates the sqlite file with its schema, all before Run() (where the
    # cwd-mismatch guard used to live) is ever constructed. Reproduced live on
    # the pilot: cwd in an unrelated repo left a real
    # <other-repo>/adws/adw_data/sssf.db on disk, populated with session rows,
    # before the eventual RuntimeError. The guard must run before that — here,
    # at the top, before anything is written.
    git_helper.assert_cwd_matches_repo(git_helper.repo_root())
    adw_id = adw_id or new_id(8)
    tracer = Tracer(cfg.observability.db,
                    f"{cfg.defaults.data_dir}/sessions/{adw_id}/events.jsonl")
    run = Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer=engineer_name())
    # #53192 round-2 (Codex finding #8): last-resort net for isolation copy
    # cleanup. Run.finish() and Run.phase()'s own exception handler are the
    # two DETERMINISTIC call sites; this covers whatever escapes both of
    # them (an exception raised outside any phase, a bare SystemExit) —
    # idempotent with both, since cleanup_run() no-ops once the directory
    # is already gone. Registered here (not in Run.__init__) so every
    # direct `Run(...)` construction in this module's own test suite does
    # NOT also register a process-wide atexit hook for a Run nobody asked
    # to be cleaned up automatically.
    atexit.register(isolation.cleanup_run, run, False)
    tracer.session_start(adw_id, run.engineer, adw_name=Path(sys.argv[0]).stem)
    # This process is the run. Record it before any phase opens, so a run that
    # hangs in its first agent call is still killable by adw_id.
    tracer.process_start(adw_id, "adw", "", os.getpid(),
                         " ".join([Path(sys.argv[0]).name, *sys.argv[1:]]))
    _finalize_when_killed(run)
    run.console.session_started(adw_id, run.engineer)
    return run
