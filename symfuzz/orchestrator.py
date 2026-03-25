"""
orchestrator.py — Coverage-directed test generation main loop.

Algorithm:
  1. Run N random clock cycles.
  2. If coverage has not improved for *stall_cycles* cycles, invoke BMC to
     find an input sequence reaching a new state.
  3. Reset the simulation, replay the BMC sequence, then resume random testing
     from the newly reached state.
  4. Repeat until full coverage or global_timeout is reached.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .bmc_interface import BmcInterface, BmcResult
from .coverage_db import CoverageDB
from .design_parser import DesignInfo
from .sim_driver import SimDriver
from .state_forcing import StateForcer


@dataclass
class CampaignResult:
    total_cycles:     int
    total_states:     int
    coverage_pct:     float
    elapsed_sec:      float
    bmc_invocations:  int
    bmc_successes:    int
    terminated_by:    str   # "full_coverage" | "timeout" | "no_more_targets"


class Orchestrator:
    def __init__(
        self,
        design:         DesignInfo,
        driver:         SimDriver,
        coverage_db:    CoverageDB,
        bmc:            BmcInterface,
        forcer:         StateForcer,
        stall_cycles:   int   = 500,
        global_timeout: float = 3600.0,
        verbose:        bool  = False,
    ):
        self.design         = design
        self.driver         = driver
        self.coverage_db    = coverage_db
        self.bmc            = bmc
        self.forcer         = forcer
        self.stall_cycles   = stall_cycles
        self.global_timeout = global_timeout
        self.verbose        = verbose

    # ------------------------------------------------------------------ #
    # Main loop                                                            #
    # ------------------------------------------------------------------ #

    def run(self) -> CampaignResult:
        start_time      = time.monotonic()
        global_cycle    = 0
        bmc_invocations = 0
        bmc_successes   = 0
        termination     = "timeout"

        # Initial reset
        self.driver.reset(cycles=5)
        initial_state = self.driver.read_state()
        self.coverage_db.record_state(initial_state, global_cycle)

        pending_replay: Optional[BmcResult] = None
        last_target:    Optional[dict]      = None
        target_retries: int                 = 0
        MAX_TARGET_RETRIES = 3

        while True:
            elapsed = time.monotonic() - start_time

            # ---- Global timeout ----------------------------------------
            if elapsed >= self.global_timeout:
                termination = "timeout"
                break

            # ---- Replay a pending BMC sequence --------------------------
            if pending_replay is not None:
                if self.verbose:
                    print(f"[orch] Replaying BMC sequence "
                          f"(depth={pending_replay.depth}) ...")
                replay_states = self.forcer.replay_sequence(pending_replay, reset_first=True)
                global_cycle = 0
                found_new = False
                for s in replay_states:
                    if self.coverage_db.record_state(s, global_cycle):
                        found_new = True
                    global_cycle += 1
                if found_new:
                    last_target    = None   # reset retry counter on progress
                    target_retries = 0
                pending_replay = None

            # ---- One random step ----------------------------------------
            state = self.driver.random_step()
            global_cycle += 1
            is_new = self.coverage_db.record_state(state, global_cycle)

            if self.verbose and is_new:
                print(f"[orch] cycle {global_cycle:6d}  "
                      f"NEW state: {state}  "
                      f"total={self.coverage_db.total_states()}  "
                      f"({self.coverage_db.coverage_pct():.1f}%)")

            # ---- Check full coverage ------------------------------------
            max_s = self.design.max_state_space
            if max_s <= (1 << 20) and self.coverage_db.total_states() >= max_s:
                termination = "full_coverage"
                break

            # ---- Coverage stall check -----------------------------------
            stall = self.coverage_db.stale_since()
            if stall < self.stall_cycles:
                continue

            # ---- Invoke BMC ---------------------------------------------
            if self.verbose:
                print(f"[orch] Stalled for {stall} cycles — invoking BMC ...")

            target = self.coverage_db.get_unvisited_neighbor_target()
            if target is None:
                if self.verbose:
                    print("[orch] No unvisited target found — terminating.")
                termination = "no_more_targets"
                break

            # Track consecutive retries on the same target.  If the replay
            # consistently fails to reach it, mark it exhausted.
            if target == last_target:
                target_retries += 1
            else:
                last_target    = target
                target_retries = 0

            if target_retries >= MAX_TARGET_RETRIES:
                self.coverage_db.mark_target_exhausted(target)
                last_target    = None
                target_retries = 0
                if self.verbose:
                    print(f"[orch] Target {target} failed {MAX_TARGET_RETRIES} "
                          f"replays — marked exhausted")
                continue

            bmc_invocations += 1
            bmc_result = self.bmc.find_sequence(target)

            if bmc_result is None:
                self.coverage_db.mark_target_exhausted(target)
                if self.verbose:
                    print(f"[orch] BMC: no path to {target} — marked exhausted")
            else:
                bmc_successes += 1
                if self.verbose:
                    print(f"[orch] BMC: found path of depth {bmc_result.depth} "
                          f"to {target}")
                pending_replay = bmc_result
                # Stall counter will reset after replay records a new state

        elapsed_total = time.monotonic() - start_time
        return CampaignResult(
            total_cycles    = global_cycle,
            total_states    = self.coverage_db.total_states(),
            coverage_pct    = self.coverage_db.coverage_pct(),
            elapsed_sec     = elapsed_total,
            bmc_invocations = bmc_invocations,
            bmc_successes   = bmc_successes,
            terminated_by   = termination,
        )
