"""
orchestrator.py — Coverage-directed test generation main loop.

Algorithm:
  1. Run N random clock cycles, RLE-tracking the applied input sequence.
  2. With probability *mutation_rate* each cycle, sample a corpus entry,
     restore its simulator state, and run a havoc or splice burst.
  3. If coverage has not improved for *stall_cycles* cycles, invoke BMC to
     find an input sequence reaching a new state.
  4. Reset the simulation, replay the BMC sequence, then resume random testing
     from the newly reached state.
  5. Any cycle that produces new coverage writes a row into the corpus.
  6. Repeat until full coverage or global_timeout is reached.
"""
from __future__ import annotations

import copy
import json
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .bmc_interface import BmcInterface, BmcResult
from .corpus_store import CorpusStore, RLESegment, append_input
from .coverage_db import CoverageDB, state_hash as compute_state_hash
from .design_parser import DesignInfo
from .mutations import havoc, splice
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
        corpus_store:   Optional[CorpusStore] = None,
        mutation_rate:  float = 0.1,
        mutation_burst: int   = 20,
        step_batch:     int   = 16,
        native_coverage: bool = False,
        coverage_poll:  int   = 10,
        uniform_sampling: bool = False,
        coverage_target: bool = True,
        stall_source:    str  = "tuple",
        log_every:       float = 30.0,
        progress_log:    Optional[str] = None,
        bmc_unproductive_limit: int = 5,
        bmc_depth_tiers: Optional[list[tuple[int, int]]] = None,
        seed_stimuli:    bool = True,
        seed_cycles:     int  = 80,
        bmc_target_rarity:    bool = False,      # lever 4
        bmc_corpus_precheck:  bool = False,      # lever 5
        seed_cross_product:   bool = False,      # lever 6
        bmc_witness_mutations: int = 0,          # lever 7
        bmc_per_reg_budget:    int = 0,          # lever 8 (0 = unlimited)
        value_class_coverage:  bool = False,     # Phase 1 — signature capture
        value_class_target:    int  = 4,         #   diversification target N
        value_class_stall:     bool = False,     # Phase 2 — stall on diversification
        value_class_bias:      bool = False,     # Phase 3-weak — seed-bias toward missing sigs
        value_class_bias_every: int = 2000,      # every N cycles
        predicate_module:      Optional[str] = None,  # differential mode 1
        differential_cross_sim: bool = False,    # differential mode 3
    ):
        self.design         = design
        self.driver         = driver
        self.coverage_db    = coverage_db
        self.bmc            = bmc
        self.forcer         = forcer
        self.stall_cycles   = stall_cycles
        self.global_timeout = global_timeout
        self.verbose        = verbose
        self.corpus         = corpus_store
        self.mutation_rate  = mutation_rate if corpus_store is not None else 0.0
        self.mutation_burst = mutation_burst
        # Clamp batch to at most stall_cycles so the stall check still fires
        # within one batch window.
        self.step_batch     = max(1, min(step_batch, max(1, stall_cycles)))
        self.native_coverage = native_coverage
        self.coverage_poll  = max(1, coverage_poll)
        self.uniform_sampling = uniform_sampling
        self.coverage_target  = coverage_target
        if stall_source not in ("tuple", "native", "any", "value_class"):
            raise ValueError(f"invalid stall_source: {stall_source!r}")
        self.stall_source     = stall_source
        self.log_every        = max(0.0, float(log_every))
        self.progress_log     = progress_log or None
        self.bmc_unproductive_limit = max(0, int(bmc_unproductive_limit))
        # Tiered (max_steps, timeout_ms) budgets for coverage-toggle BMC.
        # Cheap shallow pass first; promote on timeout/UNSAT; blacklist only
        # after top tier fails. None → single-tier fallback using the
        # BmcInterface's constructor defaults.
        self.bmc_depth_tiers: list[tuple[int, int]] = (
            list(bmc_depth_tiers) if bmc_depth_tiers else
            [(32, 5_000), (64, 15_000), (128, 60_000)]
        )
        self.seed_stimuli = bool(seed_stimuli)
        self.seed_cycles  = max(1, int(seed_cycles))
        self.bmc_target_rarity     = bool(bmc_target_rarity)
        self.bmc_corpus_precheck   = bool(bmc_corpus_precheck)
        self.seed_cross_product    = bool(seed_cross_product)
        self.bmc_witness_mutations = max(0, int(bmc_witness_mutations))
        self.bmc_per_reg_budget    = max(0, int(bmc_per_reg_budget))
        # Lever 8 bookkeeping: attempts per arch_name in the current campaign.
        self._reg_attempts: dict[str, int] = {}
        # Value-class Phase 1 state.
        self.value_class_coverage = bool(value_class_coverage)
        self.value_class_target   = max(1, int(value_class_target))
        self.value_class_stall    = bool(value_class_stall)
        self._sig_buffer: list[bytes] = []   # signatures observed since last poll
        self._sig_buf_max = 4096
        self._last_diversified_cycle: int = 0
        self._last_diversified_count: int = 0
        # Phase 3-weak state.
        self.value_class_bias       = bool(value_class_bias)
        self.value_class_bias_every = max(1, int(value_class_bias_every))
        self._last_bias_cycle       = 0
        # Differential mode 1 (predicate plugin).
        self._predicate_fn = None
        self._violation_fh = None
        if predicate_module:
            self._predicate_fn = self._load_predicate_module(predicate_module)
        # Differential mode 3 (cross-sim). Second driver attached by CLI.
        self.differential_cross_sim = bool(differential_cross_sim)
        self.cross_driver = None
        self._xdiff_log_fh = None

        # Progress-tick state.
        self._prog_last_t       = 0.0
        self._prog_last_cycle   = 0
        self._prog_mut_count    = 0
        self._prog_fh           = None  # opened lazily
        self._rng           = random.Random()
        self._batches_since_poll = 0

        # RLE log of inputs applied since the last reset/restore. Drives corpus
        # entries. Lossless run-length encoding on the applied input stream.
        self.current_path: list[RLESegment] = []

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _random_inputs(self) -> dict[str, int]:
        inputs: dict[str, int] = {}
        for p in self.design.data_inputs:
            inputs[p.name] = self._rng.randint(0, p.mask)
        if self.design.reset_port:
            rp = self.design.reset_port
            inputs[rp] = 1 if rp.endswith(("_n", "_ni", "_b")) else 0
        return inputs

    @staticmethod
    def _load_predicate_module(path: str):
        """Load a Python file and return its ``check`` callable.
        Expected signature: check(state: dict, inputs: dict) -> bool | None
        Return False or raise to signal a violation."""
        import importlib.util, os
        spec = importlib.util.spec_from_file_location(
            "symfuzz_user_predicate", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load predicate module: {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, "check", None)
        if not callable(fn):
            raise RuntimeError(f"{path}: module must define check(state, inputs)")
        return fn

    def _invoke_predicate(self, state: dict, inputs: dict,
                          cycle: int, source: str) -> None:
        if self._predicate_fn is None:
            return
        try:
            ok = self._predicate_fn(state, inputs)
        except Exception as e:
            ok = False
            reason = f"exception: {type(e).__name__}: {e}"
        else:
            reason = "returned False"
        if ok is False:
            rec = {"cycle": cycle, "source": source,
                   "state": state, "inputs": inputs, "reason": reason}
            if self._violation_fh is not None:
                try:
                    self._violation_fh.write(json.dumps(rec) + "\n")
                    self._violation_fh.flush()
                except Exception:
                    pass
            if self.verbose:
                print(f"[orch] PREDICATE VIOLATION @cy{cycle} ({source}): {reason}")

    def _record_signature(self, inputs: dict[str, int]) -> None:
        if not self.value_class_coverage:
            return
        from .value_classes import signature_for
        sig = signature_for(inputs, self.design.data_inputs)
        self._sig_buffer.append(sig)
        if len(self._sig_buffer) > self._sig_buf_max:
            # Drop oldest half to keep memory bounded. Coverage attribution
            # is coarse by design; a bin newly covered in a poll window is
            # attributed to the signatures within that window, not older.
            self._sig_buffer = self._sig_buffer[-self._sig_buf_max // 2:]

    def _step_and_track(self, inputs: dict[str, int]) -> dict[str, int]:
        """Apply *inputs* via the driver and RLE-append to current_path."""
        state = self.driver.step(inputs)
        append_input(self.current_path, inputs)
        self._record_signature(inputs)
        if self.cross_driver is not None:
            self._cross_check_step(inputs, state)
        self._invoke_predicate(state, inputs, self.coverage_db._cycle, "step")
        return state

    def _cross_check_step(self, inputs: dict, primary_state: dict) -> None:
        """Mirror *inputs* to the cross-sim driver and compare states.
        Divergence is logged to differential.jsonl."""
        try:
            sec_state = self.cross_driver.step(inputs)
        except Exception as e:
            if self.verbose:
                print(f"[orch] cross-sim step failed: {e}")
            return
        if sec_state == primary_state:
            return
        diffs = {k: (primary_state.get(k), sec_state.get(k))
                 for k in set(primary_state) | set(sec_state)
                 if primary_state.get(k) != sec_state.get(k)}
        rec = {"cycle": self.coverage_db._cycle, "inputs": inputs,
               "diffs": diffs}
        if self._xdiff_log_fh is not None:
            try:
                self._xdiff_log_fh.write(json.dumps(rec) + "\n")
                self._xdiff_log_fh.flush()
            except Exception:
                pass
        if self.verbose:
            print(f"[orch] CROSS-SIM DIVERGENCE: {diffs}")

    def _batch_step_and_track(
        self, inputs_list: list[dict[str, int]]
    ) -> tuple[list[dict[str, int]], "str | None"]:
        """
        Apply *inputs_list* via driver.step_trace and RLE-append each input
        to current_path in order. Returns (states, optional_error). On a
        partial-batch error, only the successfully-executed inputs are
        appended to current_path (same count as returned states).
        """
        if not inputs_list:
            return [], None
        states, err = self.driver.step_trace(inputs_list)
        for inp in inputs_list[:len(states)]:
            append_input(self.current_path, inp)
            self._record_signature(inp)
        return states, err

    def _bmc_steps_to_cycle_inputs(
        self, bmc_steps: list[dict]
    ) -> list[dict]:
        """
        Convert BMC's alternating (clk=0, clk=1) trace into one input-dict
        per clock cycle, using the NEGEDGE step's non-clk inputs. This
        aligns clk2fflogic's "inputs at step k drive transition k→k+1"
        semantic with the simulator's "inputs at posedge drive the
        transition" semantic — without this off-by-one fix, the simulator
        latches the WRONG inputs at each posedge and the replay diverges
        from BMC's SAT witness.

        A trailing negedge with no matching posedge is dropped (BMC's goal
        is asserted at the final posedge state).
        """
        clk = self.design.clock_port
        cycles: list[dict] = []
        prev: Optional[dict] = None
        prev_clk: Optional[int] = None
        for step in bmc_steps:
            cur_clk = int(step.get(clk, 0))
            if prev is not None and prev_clk == 0 and cur_clk == 1:
                cycles.append({k: v for k, v in prev.items() if k != clk})
            prev = step
            prev_clk = cur_clk
        return cycles

    def _batch_step_and_track_raw(
        self, inputs_list: list[dict[str, int]]
    ) -> tuple[list[dict[str, int]], "str | None"]:
        """Raw-step variant used by BMC replay. Each input dict drives every
        port (including clk) with the caller-specified polarity and advances
        the simulator by a single SMT2 transition. Preserves clk2fflogic's
        alternating negedge/posedge semantics so BMC's SAT witness
        reproduces faithfully."""
        if not inputs_list:
            return [], None
        states, err = self.driver.step_raw_trace(inputs_list)
        for inp in inputs_list[:len(states)]:
            append_input(self.current_path, inp)
        return states, err

    def _corpus_state_matching(
        self, target: dict[str, dict | int]
    ) -> Optional[dict[str, int]]:
        """Lever 5: scan visited states for one whose register values satisfy
        *target*. Returns the full register state dict to force, or None."""
        try:
            rows = self.coverage_db._conn.execute(
                "SELECT state_json FROM visited_states LIMIT 5000"
            ).fetchall()
        except Exception:
            return None
        import json as _json
        for (sj,) in rows:
            try:
                state = _json.loads(sj)
            except Exception:
                continue
            ok = True
            for reg_name, spec in target.items():
                reg_val = state.get(reg_name)
                if reg_val is None:
                    ok = False; break
                if isinstance(spec, dict):
                    bit = int(spec.get("bit", 0))
                    val = int(spec.get("val", 0))
                    if ((int(reg_val) >> bit) & 1) != val:
                        ok = False; break
                else:
                    if int(reg_val) != int(spec):
                        ok = False; break
            if ok:
                return {k: int(v) for k, v in state.items()}
        return None

    def _mutate_witness_steps(
        self, steps: list[dict[str, int]]
    ) -> list[dict[str, int]]:
        """Lever 7: produce a mutated copy of a BMC witness step list.
        Flips a single bit in one randomly-chosen input of one cycle.
        Conservative — stays close to the SAT witness so the targeted
        bin is still likely to toggle."""
        if not steps:
            return []
        out = [dict(s) for s in steps]
        clk = self.design.clock_port
        rst = self.design.reset_port
        cycle_idx = self._rng.randrange(len(out))
        ports = [k for k in out[cycle_idx].keys()
                 if k != clk and k != rst]
        if not ports:
            return out
        p = self._rng.choice(ports)
        width = next((x.width for x in self.design.data_inputs if x.name == p), 1)
        bit = self._rng.randrange(max(1, width))
        out[cycle_idx][p] = out[cycle_idx].get(p, 0) ^ (1 << bit)
        return out

    @staticmethod
    def _extreme_values(width: int) -> list[int]:
        """Interesting pinned values for a port of given width — covers
        zero, one, all-ones, MSB-only, alternating-bit patterns, and mid.
        Deduplicated and masked to the port width."""
        mask = (1 << width) - 1
        msb  = 1 << (width - 1) if width > 0 else 0
        vals = {
            0,
            1 & mask,
            mask,                       # all-ones / -1
            msb,                        # MSB only
            mask ^ msb,                 # all-ones except MSB (INT_MAX)
            0xAAAAAAAAAAAAAAAA & mask,  # 1010...
            0x5555555555555555 & mask,  # 0101...
        }
        return sorted(vals)

    def _run_diversification_pass(self, global_cycle: int) -> int:
        """Phase-3-weak: pick an under-diversified covered bin, generate a
        few inputs whose signatures are NOT yet seen for that bin, and apply
        them for a short burst. Advances global_cycle by the inputs applied."""
        if not (self.value_class_coverage and self.value_class_bias):
            return global_cycle
        from .value_classes import missing_signatures, inputs_for_signature
        pick = self.coverage_db.get_under_diversified_point(
            self.value_class_target)
        if pick is None:
            return global_cycle
        pid, seen = pick
        n_ports = len(self.design.data_inputs)
        new_sigs = missing_signatures(seen, n_ports, limit=8)
        if not new_sigs:
            return global_cycle
        reset_port = self.design.reset_port
        reset_deasserted = (1 if reset_port and
                            reset_port.endswith(("_n", "_ni", "_b")) else 0)
        batch = []
        for sig in new_sigs:
            base = inputs_for_signature(sig, self.design.data_inputs, self._rng)
            if reset_port:
                base[reset_port] = reset_deasserted
            # Hold each missing-signature input for a few cycles so regs settle.
            for _ in range(4):
                batch.append(base)
        if self.verbose:
            print(f"[orch] vc-bias pass: point={pid} +{len(new_sigs)} "
                  f"missing sigs × 4 cycles")
        states, err = self._batch_step_and_track(batch)
        for i, st in enumerate(states):
            self._record(st, global_cycle + i, source="vc_bias")
        if err and self.verbose:
            print(f"[orch] vc-bias partial: {err}")
        return global_cycle + len(states)

    def _run_seed_stimuli(self, global_cycle: int) -> int:
        """Inject structured extreme-value stimuli before the main loop.
        For each (data-input, extreme-value) pair, reset the sim and apply
        ``self.seed_cycles`` cycles with that port pinned while the other
        data inputs draw random values — gives the datapath registers a
        deterministic way to toggle high-order bits without relying on
        random discovery. Returns the advanced cycle count."""
        if not self.seed_stimuli:
            return global_cycle
        reset_port = self.design.reset_port
        reset_deasserted = (1 if reset_port and
                            reset_port.endswith(("_n", "_ni", "_b")) else 0)
        seeds: list[tuple[str, int, int]] = []   # (port_name, width, value)
        for p in self.design.data_inputs:
            for v in self._extreme_values(p.width):
                seeds.append((p.name, p.width, v))
        # Lever 6: cross-product of extremes across data-input pairs. Encodes
        # coordinated extreme patterns (e.g. op_a=INT_MIN ∧ opcode=SDIV) as
        # synthetic dual-pin seeds. Second port stored in extra dict.
        pair_seeds: list[tuple[dict[str, int]]] = []
        if self.seed_cross_product:
            inputs = list(self.design.data_inputs)
            for i in range(len(inputs)):
                for j in range(i + 1, len(inputs)):
                    a, b = inputs[i], inputs[j]
                    for va in self._extreme_values(a.width):
                        for vb in self._extreme_values(b.width):
                            pair_seeds.append(({a.name: va, b.name: vb},))
            if self.verbose:
                print(f"[orch] seed cross-product: +{len(pair_seeds)} pair seeds")
        if self.verbose:
            print(f"[orch] seed stimuli: {len(seeds)} pinned-port runs "
                  f"× {self.seed_cycles} cycles")
        for label_port, _w, pin_val in seeds:
            self.driver.reset(cycles=5)
            self.current_path = []
            batch: list[dict[str, int]] = []
            for _ in range(self.seed_cycles):
                inp = self._random_inputs()
                inp[label_port] = pin_val
                if reset_port:
                    inp[reset_port] = reset_deasserted
                batch.append(inp)
            states, err = self._batch_step_and_track(batch)
            for i, st in enumerate(states):
                self._record(st, global_cycle + i, source="seed")
            global_cycle += len(states)
            if err and self.verbose:
                print(f"[orch] seed {label_port}={pin_val} partial: {err}")
        for (pins,) in pair_seeds:
            self.driver.reset(cycles=5)
            self.current_path = []
            batch = []
            for _ in range(self.seed_cycles):
                inp = self._random_inputs()
                for k, v in pins.items():
                    inp[k] = v
                if reset_port:
                    inp[reset_port] = reset_deasserted
                batch.append(inp)
            states, err = self._batch_step_and_track(batch)
            for i, st in enumerate(states):
                self._record(st, global_cycle + i, source="seed_pair")
            global_cycle += len(states)
        return global_cycle

    def _record(self, state: dict[str, int], cycle: int, source: str,
                contribution: int = 1) -> bool:
        is_new = self.coverage_db.record_state(state, cycle)
        if is_new:
            if self.verbose:
                print(f"[orch] cycle {cycle:6d}  NEW state: {state}  "
                      f"total={self.coverage_db.total_states()}  "
                      f"({self.coverage_db.coverage_pct():.1f}%)")
            if self.corpus is not None:
                h = compute_state_hash(state)
                self.corpus.add_entry(h, self.current_path, source, self.driver,
                                      contribution=contribution)
        return is_new

    def _progress_tick(self, global_cycle: int, start_wall: float,
                       bmc_invocations: int, bmc_successes: int) -> None:
        """
        Periodically emit a [prog] line and append a JSON record to the
        progress log. Called from the main loop on every iteration; a
        wall-clock check keeps the per-iteration cost near zero.
        """
        if self.log_every <= 0 and not self.progress_log:
            return
        now = time.monotonic()
        elapsed = now - start_wall
        interval = now - self._prog_last_t
        if self._prog_last_t == 0.0:
            # First tick deferred until we've actually run log_every seconds.
            if elapsed < self.log_every:
                return
            self._prog_last_t = start_wall
            interval = elapsed
        elif self.log_every > 0 and interval < self.log_every:
            return

        # global_cycle resets to 0 after BMC replays; treat a decrease as
        # zero throughput for display purposes.
        delta_cyc = max(0, global_cycle - self._prog_last_cycle)
        cps = delta_cyc / interval if interval > 0 else 0.0

        states = self.coverage_db.total_states()
        covered = getattr(self.coverage_db, "native_covered", lambda: 0)()
        total_native = getattr(self.coverage_db, "_latest_native_total", None)
        nat_pct = (100.0 * covered / total_native) if total_native else 0.0
        corpus_sz = 0
        if self.corpus is not None:
            corpus_sz = self.corpus._conn.execute(
                "SELECT COUNT(*) FROM corpus_entries").fetchone()[0]

        cov_calls = getattr(self.driver, "total_cov_calls", 0) or 1
        cov_avg_ms = getattr(self.driver, "total_cov_duration_ms", 0.0) / cov_calls

        bmc_calls = self.bmc.total_calls or 1
        bmc_avg_s = (self.bmc.total_duration_ms / 1000.0) / bmc_calls

        vc_suffix = ""
        if self.value_class_coverage:
            div, tot = self.coverage_db.value_class_diversification(
                self.value_class_target)
            vc_pct = (100.0 * div / tot) if tot else 0.0
            vc_suffix = f"  vc={div}/{tot}({vc_pct:.1f}%)"
            # Track growth for value_class-stall semantics.
            if div > self._last_diversified_count:
                self._last_diversified_count = div
                self._last_diversified_cycle = global_cycle

        line = (f"[prog] t={elapsed:7.1f}s  cyc={global_cycle:7d}  "
                f"cps={cps:6.1f}  states={states:6d}  "
                f"nat={covered}/{total_native or '?'} "
                f"({nat_pct:.1f}%)  corpus={corpus_sz}  "
                f"bmc={bmc_successes}/{bmc_invocations} avg={bmc_avg_s:.2f}s  "
                f"mut={self._prog_mut_count}  "
                f"poll={cov_avg_ms:.0f}ms{vc_suffix}")
        print(line, file=sys.stderr, flush=True)

        if self.progress_log:
            if self._prog_fh is None:
                self._prog_fh = open(self.progress_log, "a", buffering=1)
            rec = {
                "t":              round(elapsed, 3),
                "cycle":          global_cycle,
                "cps":            round(cps, 2),
                "states":         states,
                "native_covered": covered,
                "native_total":   total_native,
                "corpus_size":    corpus_sz,
                "bmc_total":      bmc_invocations,
                "bmc_ok":         bmc_successes,
                "mutations":      self._prog_mut_count,
                "xcrg_avg_ms":    round(cov_avg_ms, 1),
                "bmc_avg_ms":     round(self.bmc.total_duration_ms / bmc_calls, 1),
            }
            self._prog_fh.write(json.dumps(rec) + "\n")

        self._prog_last_t = now
        self._prog_last_cycle = global_cycle

    def _poll_native_coverage(self, global_cycle: int,
                              force: bool = False) -> int:
        """Poll the driver for native coverage if enabled and the batch
        counter has elapsed. Returns the number of newly-covered points.
        *force=True* bypasses the batch-cadence gate; used after BMC replays
        so the productivity check sees any new native points immediately."""
        if not self.native_coverage:
            return 0
        if not force:
            self._batches_since_poll += 1
            if self._batches_since_poll < self.coverage_poll:
                return 0
        self._batches_since_poll = 0
        snap = self.driver.collect_coverage()
        if snap is None:
            return 0
        backend = getattr(self.driver, "backend_name", "unknown")
        new = self.coverage_db.record_coverage_snapshot(snap, global_cycle, backend)
        if new and self.verbose:
            print(f"[orch] native coverage +{new} points "
                  f"({self.coverage_db.native_summary()})")
        # Phase 1: attribute signatures in the current window to every
        # newly-covered point. Coarse but cheap; poll windows are small so
        # the attribution is tight enough for practical use.
        if (self.value_class_coverage and new
                and getattr(self.coverage_db, "last_new_point_ids", None)):
            window_sigs = set(self._sig_buffer)
            for pid in self.coverage_db.last_new_point_ids:
                for sig in window_sigs:
                    self.coverage_db.record_value_class_hit(
                        pid, sig, global_cycle)
            self.coverage_db._conn.commit()
        # Start a fresh attribution window after each poll.
        if self.value_class_coverage:
            self._sig_buffer.clear()
        # If any new native points were observed and the corpus is live,
        # snapshot the current path as a corpus entry under 'native'. The
        # contribution score propagates the count of newly-covered points
        # so weighted sampling can prefer this entry later.
        if new and self.corpus is not None and self.current_path:
            from .coverage_db import state_hash as _h
            try:
                state = self.driver.read_state()
                h = _h(state)
                self.corpus.add_entry(h, self.current_path, "native", self.driver,
                                      contribution=new)
            except RuntimeError:
                pass
        return new

    def _wire_name_cache(self) -> dict[str, int]:
        """Build a {name: width} map over all BMC-targetable combinational
        wires (plus inputs/outputs that aren't already registers). Used to
        classify coverage points whose signal isn't in `design.registers`
        as Class 3 (reach-a-value targets rather than flip)."""
        if getattr(self, "_design_wire_names_cache", None) is not None:
            return self._design_wire_names_cache
        cache: dict[str, int] = {}
        for p in list(self.design.inputs) + list(self.design.outputs) \
                 + list(getattr(self.design, "wires", [])):
            if p.name:
                cache.setdefault(p.name, int(p.width))
        self._design_wire_names_cache = cache
        return cache

    @staticmethod
    def _direct_bit(state: dict, sig: str, bit: int) -> Optional[int]:
        """Read bit `bit` of `sig` from a driver.read_state() dict.

        Returns the bit value (0 or 1), or None when the signal is not in
        the state dict — e.g. a combinational wire that the harness doesn't
        expose. The orchestrator then falls back to the coverage-DB signal.
        """
        if sig not in state:
            return None
        return (int(state[sig]) >> bit) & 1

    def _toggle_target_to_bmc_target(self, info: dict) -> Optional[dict]:
        """
        Translate a coverage-driven toggle point into a BMC constraint.

        Three cases, design-agnostic:

        - Flop-backed (top-level or submodule register — signal appears in
          ``design.registers`` with the given fully-qualified arch_name):
          emit ``{sig: {"bit": K, "val": V, "flip": True}}``. The flip
          constraint forces BMC to find a trace where the bit *transitions*
          to V at the final step, not merely holds V.

        - Combinational wire (signal is a known input / output / internal
          wire but not a register): emit ``{sig: {"bit": K, "val": V}}``
          without flip. Flip semantics are meaningless on non-stateful
          signals; reaching the value is the appropriate goal.

        - Unresolvable: return ``None`` so the orchestrator falls back to
          the legacy register-value picker.
        """
        sig = info.get("reg")
        if not sig:
            return None
        bit_s = info.get("bit", 0)
        bit = 0 if bit_s in (None, "*") else int(bit_s)
        direction = info.get("dir", "rise")
        val = 1 if direction == "rise" else 0

        # Class 1 / 2: flop-backed signal.
        reg = next((r for r in self.design.registers
                    if r.arch_name == sig), None)
        if reg is not None:
            if bit >= reg.width:
                return None
            if self.verbose:
                print(f"[orch] coverage-target classify: sig={sig} "
                      f"bit={bit} → flop (flip)")
            return {sig: {"bit": bit, "val": val, "flip": True}}

        # Class 3: combinational wire / input / output. Reach a value, no
        # flip (combinational signals transition freely; a flip constraint
        # would be enforced only between adjacent SMT2 steps and often
        # UNSATs trivially).
        wires = self._wire_name_cache()
        if sig in wires:
            if bit >= wires[sig]:
                return None
            if self.verbose:
                print(f"[orch] coverage-target classify: sig={sig} "
                      f"bit={bit} → wire (reach-value)")
            return {sig: {"bit": bit, "val": val}}

        if self.verbose:
            print(f"[orch] coverage-target classify: sig={sig} "
                  f"bit={bit} → UNRESOLVED (falling back)")
        return None

    def _try_startup_restore(self) -> bool:
        """
        If the corpus has a prior entry, try to restore into it so we resume
        near the frontier instead of starting from reset. Returns True on
        successful restore.
        """
        if self.corpus is None:
            return False
        entry = self.corpus.pick_deepest()
        if entry is None:
            return False
        if self.verbose:
            print(f"[orch] Resuming from corpus entry (depth={entry.total_depth}, "
                  f"source={entry.source})")
        if self.corpus.restore(entry.state_hash, self.driver):
            self.current_path = copy.deepcopy(entry.segments)
            return True
        return False

    def _mutation_step(self, global_cycle: int) -> tuple[int, bool]:
        """
        With probability *mutation_rate*, run one mutation burst as a single
        batched step_trace call. Returns (cycles_consumed, found_new_coverage).
        """
        if self.corpus is None or self.mutation_rate <= 0.0:
            return 0, False
        if self._rng.random() >= self.mutation_rate:
            return 0, False

        seed = self.corpus.sample(self._rng, uniform=self.uniform_sampling)
        if seed is None:
            return 0, False
        if not self.corpus.restore(seed.state_hash, self.driver):
            return 0, False

        # Seed the RLE path so subsequent corpus entries know the full prefix.
        self.current_path = copy.deepcopy(seed.segments)

        strategy = self._rng.choice(("havoc", "splice"))
        other: Optional[object] = None
        if strategy == "splice":
            other = self.corpus.sample(self._rng, uniform=self.uniform_sampling)
            if other is None or other.state_hash == seed.state_hash:
                strategy = "havoc"

        if strategy == "havoc":
            inputs_list = list(havoc(self.design, self.mutation_burst, self._rng))
        else:
            inputs_list = list(splice(self.design, other, self.mutation_burst, self._rng))

        if not inputs_list:
            return 0, False

        states, err = self._batch_step_and_track(inputs_list)
        found_new = False
        for state in states:
            global_cycle += 1
            if self._record(state, global_cycle, source="mutation"):
                found_new = True
        if err is not None:
            raise RuntimeError(f"mutation burst: {err}")
        self._prog_mut_count += 1
        return len(states), found_new

    # ------------------------------------------------------------------ #
    # Main loop                                                            #
    # ------------------------------------------------------------------ #

    def run(self) -> CampaignResult:
        start_time      = time.monotonic()
        global_cycle    = 0
        bmc_invocations = 0

        # Open differential log files if enabled.
        try:
            out_dir = Path(self.progress_log).parent if self.progress_log else Path(".")
        except Exception:
            out_dir = Path(".")
        if self._predicate_fn is not None:
            self._violation_fh = open(out_dir / "violations.jsonl",
                                      "a", buffering=1)
        if self.cross_driver is not None:
            self._xdiff_log_fh = open(out_dir / "differential.jsonl",
                                      "a", buffering=1)
        bmc_successes   = 0
        termination     = "timeout"

        # ---- Initial state (reset, or restore if corpus has history) ----
        resumed = self._try_startup_restore()
        if not resumed:
            self.driver.reset(cycles=5)
            self.current_path = []
        initial_state = self.driver.read_state()
        self._record(initial_state, global_cycle, source="random")

        # Structured-seed injection before the main loop. Runs deterministic
        # pinned-port stimuli so extreme datapath bits toggle without
        # requiring BMC or random discovery. Skipped if the corpus was
        # restored (resumed campaign already has state).
        if not resumed and self.seed_stimuli:
            global_cycle = self._run_seed_stimuli(global_cycle)
            # Reset cleanly after seeds so the main loop starts from s_0.
            self.driver.reset(cycles=5)
            self.current_path = []

        pending_replay:      Optional[BmcResult] = None
        pending_point_id:    Optional[str]       = None
        pending_target_kind: str                 = "register"
        last_target:         Optional[dict]      = None
        target_retries:    int                 = 0
        unproductive_streak: int               = 0
        MAX_TARGET_RETRIES = 3

        while True:
            elapsed = time.monotonic() - start_time

            # ---- Global timeout ----------------------------------------
            if elapsed >= self.global_timeout:
                termination = "timeout"
                break

            # ---- Replay a pending BMC sequence (single batched call) ----
            if pending_replay is not None:
                if self.verbose:
                    print(f"[orch] Replaying BMC sequence "
                          f"(depth={pending_replay.depth}) ...")
                # Productivity snapshot: compare states + native coverage
                # before and after the replay. A SAT BMC target that
                # contributes nothing new is retired immediately; a streak
                # of them terminates the campaign.
                states_before = self.coverage_db.total_states()
                native_before = self.coverage_db.native_covered()

                self.driver.reset(cycles=5)
                # Force every tracked register to 0 so the simulator starts
                # replay at a state matching BMC's ``assume_zero_init=true``
                # s_0 exactly. Without this, reset leaves some flops at
                # non-zero values (e.g. initial-value side effects) that
                # diverge from BMC's SMT model and cause full-trace replay
                # to miss the witness.
                try:
                    zero_state = {r.arch_name: 0 for r in self.design.registers}
                    if zero_state:
                        self.driver.force(zero_state)
                except RuntimeError as e:
                    if self.verbose:
                        print(f"[orch] force(zero_state) failed: {e}")
                # Snapshot the targeted register's pre-replay value so the
                # direct-probe check (below) can confirm a flip actually
                # happened, independent of the coverage reporter.
                try:
                    reg_snap_before = self.driver.read_state()
                except RuntimeError:
                    reg_snap_before = {}
                self.current_path = []
                global_cycle = 0
                # Cycle-wrapped replay: pair BMC's (clk=0, clk=1) steps and
                # feed the NEGEDGE step's inputs through one full-cycle
                # `step` per pair. Aligns clk2fflogic's "inputs at step k
                # drive transition k→k+1" semantic with Verilator/xsim's
                # "inputs at posedge drive the transition" semantic.
                cycle_inputs = self._bmc_steps_to_cycle_inputs(pending_replay.steps)
                filtered_list = []
                for inp in cycle_inputs:
                    full = {}
                    for p in self.design.data_inputs:
                        full[p.name] = 0
                    full.update(inp)
                    filtered_list.append(full)
                if self.verbose:
                    print(f"[orch] replay: {len(filtered_list)} full cycles "
                          f"(from {len(pending_replay.steps)} SMT2 steps)")
                states, err = self._batch_step_and_track(filtered_list)
                found_new = False
                for state in states:
                    global_cycle += 1
                    if self._record(state, global_cycle, source="bmc"):
                        found_new = True
                if err is not None:
                    raise RuntimeError(f"BMC replay: {err}")

                # Force-poll native coverage so the productivity check sees
                # any points unlocked by this replay (the regular poll only
                # runs after random-step batches).
                self._poll_native_coverage(global_cycle, force=True)

                # Direct signal probe: read the post-replay state and check
                # if the targeted register's bit is at the requested value.
                # This bypasses the coverage reporter entirely — if
                # direct_hit != target_hit, the reporter has a blind spot
                # we need to diagnose.
                try:
                    reg_snap_after = self.driver.read_state()
                except RuntimeError:
                    reg_snap_after = {}

                direct_hit: Optional[bool] = None
                if pending_target_kind == "coverage_toggle" and pending_replay.target:
                    sig  = next(iter(pending_replay.target))
                    spec = pending_replay.target[sig]
                    if isinstance(spec, dict):
                        tbit = int(spec["bit"])
                        tval = int(spec["val"])
                        b_before = self._direct_bit(reg_snap_before, sig, tbit)
                        b_after  = self._direct_bit(reg_snap_after,  sig, tbit)
                        if b_after is None:
                            direct_hit = None
                        elif spec.get("flip"):
                            direct_hit = (b_after == tval) and (b_before == (tval ^ 1))
                        else:
                            direct_hit = (b_after == tval)

                # Post-replay productivity check.
                native_after = self.coverage_db.native_covered()
                states_after = self.coverage_db.total_states()
                native_delta = native_after - native_before
                states_delta = states_after - states_before
                target_hit = (pending_point_id is not None and
                              self.coverage_db.is_native_covered(pending_point_id))

                if pending_target_kind == "coverage_toggle":
                    # Prefer the direct probe; fall back to the coverage
                    # reporter when the signal isn't in the read_state dict
                    # (e.g. a combinational wire the harness doesn't expose).
                    productive = direct_hit if direct_hit is not None else target_hit
                else:
                    productive = (states_delta > 0) or (native_delta > 0)

                if self.verbose and direct_hit is not None and direct_hit != target_hit:
                    print(f"[orch] coverage-reporter mismatch on "
                          f"{list(pending_replay.target.items())}  "
                          f"direct={direct_hit}  cov={target_hit}")

                # Append the BMC record now that productivity is known. UNSAT
                # records are written by BmcInterface; SAT records are
                # written here so they include the productivity fields.
                self.bmc._log_record({
                    "t":               time.monotonic(),
                    "outcome":         "sat",
                    "kind":            pending_target_kind,
                    "target":          pending_replay.target,
                    "duration_ms":     round(self.bmc.last_duration_ms, 1),
                    "depth":           pending_replay.depth,
                    "target_point_id": pending_point_id,
                    "target_hit":      target_hit,
                    "direct_hit":      direct_hit,
                    "productive":      productive,
                    "native_delta":    native_delta,
                    "states_delta":    states_delta,
                })

                if productive:
                    last_target = None
                    target_retries = 0
                    unproductive_streak = 0
                    # Lever 7: replay mutated variants of the witness to
                    # squeeze more coverage per solve. Each variant flips one
                    # bit in one cycle's inputs.
                    if (self.bmc_witness_mutations > 0
                            and pending_replay is not None
                            and pending_replay.steps):
                        cycle_inputs = self._bmc_steps_to_cycle_inputs(
                            pending_replay.steps)
                        for m in range(self.bmc_witness_mutations):
                            mutated = self._mutate_witness_steps(cycle_inputs)
                            try:
                                self.driver.reset(cycles=5)
                                self.current_path = []
                                mstates, merr = self._batch_step_and_track(mutated)
                                for i, st in enumerate(mstates):
                                    self._record(st, global_cycle + i,
                                                 source="bmc_mutation")
                                global_cycle += len(mstates)
                                if merr and self.verbose:
                                    print(f"[orch] witness-mut {m}: {merr}")
                            except Exception as e:
                                if self.verbose:
                                    print(f"[orch] witness-mut {m} failed: {e}")
                else:
                    # SAT but the targeted point wasn't actually exercised.
                    # If the point hasn't reached top tier yet, promote
                    # instead of blacklisting — a deeper witness may reach
                    # the toggle a shallow one can't. Promotion counts as
                    # forward motion, so don't increment the streak.
                    self.coverage_db.mark_target_exhausted(pending_replay.target)
                    promoted = False
                    if (pending_point_id
                            and pending_target_kind == "coverage_toggle"
                            and self.bmc_depth_tiers):
                        top = len(self.bmc_depth_tiers) - 1
                        promoted = self.coverage_db.promote_coverage_point(
                            pending_point_id, max_tier=top)
                    if promoted:
                        # Forward motion: reset streak rather than increment.
                        unproductive_streak = 0
                        if self.verbose:
                            new_tier = self.coverage_db.get_point_tier(
                                pending_point_id)
                            print(f"[orch] SAT but unproductive → "
                                  f"promoted to tier {new_tier}: "
                                  f"{pending_replay.target} "
                                  f"(point {pending_point_id})")
                    else:
                        if pending_point_id:
                            self.coverage_db.mark_coverage_point_exhausted(
                                pending_point_id)
                        unproductive_streak += 1
                        if self.verbose:
                            print(f"[orch] SAT but unproductive → exhausted: "
                                  f"{pending_replay.target}  "
                                  f"(streak={unproductive_streak})")
                pending_replay = None
                pending_point_id = None
                pending_target_kind = "register"

                if (self.bmc_unproductive_limit > 0 and
                        unproductive_streak >= self.bmc_unproductive_limit):
                    if self.verbose:
                        print(f"[orch] {unproductive_streak} consecutive "
                              f"unproductive BMC calls — terminating.")
                    termination = "no_productive_bmc"
                    break

            # ---- Optional mutation burst --------------------------------
            consumed, mut_new = self._mutation_step(global_cycle)
            global_cycle += consumed
            if mut_new:
                continue

            # ---- Batched random steps -----------------------------------
            batch_n = self.step_batch
            inputs_list = [self._random_inputs() for _ in range(batch_n)]
            states, err = self._batch_step_and_track(inputs_list)
            for state in states:
                global_cycle += 1
                self._record(state, global_cycle, source="random")
            if err is not None:
                raise RuntimeError(f"random batch: {err}")

            # ---- Native-coverage poll -----------------------------------
            self._poll_native_coverage(global_cycle)

            # ---- Periodic progress line + JSONL --------------------------
            self._progress_tick(global_cycle, start_time,
                                bmc_invocations, bmc_successes)

            # ---- Check full coverage ------------------------------------
            max_s = self.design.max_state_space
            if max_s <= (1 << 20) and self.coverage_db.total_states() >= max_s:
                termination = "full_coverage"
                break
            # Native-coverage completion short-circuit (works on any design
            # size, including CVA6-scale where the register-tuple space is
            # astronomical).
            if self.native_coverage and self.coverage_db.is_native_complete():
                termination = "full_coverage"
                if self.verbose:
                    print("[orch] native coverage 100% — terminating.")
                break

            # ---- Coverage stall check -----------------------------------
            if self.stall_source == "value_class" and self.value_class_coverage:
                stall = global_cycle - self._last_diversified_cycle
            else:
                stall = self.coverage_db.stale_since(self.stall_source)
            # Phase-3-weak diversification pass — periodic, independent of stall.
            if (self.value_class_bias
                    and global_cycle - self._last_bias_cycle
                        >= self.value_class_bias_every):
                self._last_bias_cycle = global_cycle
                global_cycle = self._run_diversification_pass(global_cycle)
            if stall < self.stall_cycles:
                continue

            # ---- Invoke BMC ---------------------------------------------
            if self.verbose:
                print(f"[orch] Stalled for {stall} cycles — invoking BMC ...")

            target = None
            target_kind = "register"
            target_point_id: Optional[str] = None
            target_tier: int = 0
            if self.coverage_target:
                cov_info = self.coverage_db.get_uncovered_coverage_target(
                    rarity_weighted=self.bmc_target_rarity,
                )
                # Lever 8: respect per-register attempt budget.
                if (cov_info and self.bmc_per_reg_budget > 0):
                    reg_name = cov_info.get("reg", "")
                    if (self._reg_attempts.get(reg_name, 0)
                            >= self.bmc_per_reg_budget):
                        if self.verbose:
                            print(f"[orch] per-reg budget exhausted for "
                                  f"{reg_name}, skipping point "
                                  f"{cov_info.get('point_id')}")
                        self.coverage_db.mark_coverage_point_exhausted(
                            cov_info.get("point_id", ""))
                        cov_info = None
                if cov_info and cov_info.get("kind") == "toggle":
                    t = self._toggle_target_to_bmc_target(cov_info)
                    if t is not None:
                        if self.verbose:
                            print(f"[orch] coverage target: "
                                  f"toggle {cov_info.get('reg')} "
                                  f"bit {cov_info.get('bit')} "
                                  f"{cov_info.get('dir')} → {t}")
                        target = t
                        target_kind = "coverage_toggle"
                        target_point_id = cov_info.get("point_id")
                        target_tier = int(cov_info.get("tier", 0) or 0)
            if target is None:
                target = self.coverage_db.get_unvisited_neighbor_target()
            if target is None:
                if self.verbose:
                    print("[orch] No unvisited target found — terminating.")
                termination = "no_more_targets"
                break

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

            # Lever 5: corpus pre-check. If any corpus state already has
            # the target bit at the goal value (for coverage_toggle kind),
            # force that state and skip the BMC call — a much cheaper path.
            if (self.bmc_corpus_precheck
                    and target_kind == "coverage_toggle"
                    and self.corpus is not None):
                matched = self._corpus_state_matching(target)
                if matched is not None:
                    try:
                        self.driver.force(matched)
                        st = self.driver.read_state()
                        self._record(st, global_cycle, source="corpus_precheck")
                        if self.verbose:
                            print(f"[orch] corpus precheck hit for {target} "
                                  f"— skipped BMC")
                        # Skip BMC entirely for this target; poll coverage
                        # will pick up the bin next tick.
                        continue
                    except Exception as e:
                        if self.verbose:
                            print(f"[orch] corpus precheck force failed: {e}")

            # Lever 8 accounting.
            if self.bmc_per_reg_budget > 0 and target:
                for reg_name in target.keys():
                    self._reg_attempts[reg_name] = \
                        self._reg_attempts.get(reg_name, 0) + 1

            bmc_invocations += 1
            # Select tiered budget only for coverage_toggle targets; other
            # target kinds (legacy neighbor fallback) use interface defaults.
            tier_steps  = None
            tier_ms     = None
            if target_kind == "coverage_toggle" and self.bmc_depth_tiers:
                tier_idx = min(target_tier, len(self.bmc_depth_tiers) - 1)
                tier_steps, tier_ms = self.bmc_depth_tiers[tier_idx]
                if self.verbose:
                    print(f"[orch] bmc tier {tier_idx} "
                          f"({tier_steps} steps, {tier_ms}ms) "
                          f"point={target_point_id}")
            bmc_result = self.bmc.find_sequence(
                target, kind=target_kind,
                max_steps=tier_steps, timeout_ms=tier_ms,
            )

            if bmc_result is None:
                self.coverage_db.mark_target_exhausted(target)
                promoted = False
                if target_point_id is not None and target_kind == "coverage_toggle":
                    top = len(self.bmc_depth_tiers) - 1
                    promoted = self.coverage_db.promote_coverage_point(
                        target_point_id, max_tier=top,
                    )
                    if not promoted:
                        self.coverage_db.mark_coverage_point_exhausted(
                            target_point_id)
                elif target_point_id is not None:
                    self.coverage_db.mark_coverage_point_exhausted(target_point_id)
                if self.verbose:
                    if promoted:
                        new_tier = self.coverage_db.get_point_tier(target_point_id)
                        print(f"[orch] BMC: no path to {target} at tier "
                              f"{target_tier} — promoted to tier {new_tier} "
                              f"(point {target_point_id})")
                    else:
                        print(f"[orch] BMC: no path to {target} — marked exhausted"
                              f"{f' (point {target_point_id})' if target_point_id else ''}")
            else:
                bmc_successes += 1
                if self.verbose:
                    print(f"[orch] BMC: found path of depth {bmc_result.depth} "
                          f"to {target}")
                pending_replay      = bmc_result
                pending_point_id    = target_point_id
                pending_target_kind = target_kind

        if self._prog_fh is not None:
            try:
                self._prog_fh.close()
            except Exception:
                pass
            self._prog_fh = None

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
