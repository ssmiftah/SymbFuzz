# SymbFuzz — Technical Reference

This is the function-level reference for SymbFuzz. For the design-level view, see [ARCHITECTURE.md](ARCHITECTURE.md). For how to use the tool, see [USER_MANUAL.md](USER_MANUAL.md).

Each section below mirrors one source file. Within a section, `class` bodies list their methods in source order. Line numbers are informational and may drift as the source evolves.

---

## 1. `symfuzz/cli.py`

Entry point. Parses command-line arguments, merges with optional YAML config, constructs the object graph (design, driver, coverage DB, BMC interface, corpus, orchestrator), runs the campaign, writes the final report.

### `_parse_bmc_tiers(spec: str) -> list[tuple[int, int]] | None`
Parses a `"32:5000,64:15000,128:60000"`-style CLI string into tier tuples. Returns `None` when the user sets `"off"`/`"none"`/empty (which disables tiering and falls back to the interface's constructor defaults). Integers are parsed strictly; malformed segments raise.

### `_write_meta(output_dir, args, design)`
Writes a `meta.json` to the output dir containing the resolved CLI args, the detected design (top module, registers, ports, widths), and the timestamp. Used for reproducibility and for human diffing of runs.

### `_write_final_coverage(output_dir, cov_db)`
After the campaign, emits `final_coverage.json` with per-kind covered counts, totals, and the full list of visited coverage-point IDs.

### `_apply_yaml_config(ap, args, argv)`
Three-tier precedence: argparse defaults < YAML values < explicit CLI flags. Reads the YAML at `args.config`, overrides any `args.<key>` that wasn't explicitly set on the command line. Unknown YAML keys raise with a clear error. The "was this set on CLI?" check walks `argv` looking for `--flag` tokens.

### `main(argv=None)`
Full campaign pipeline:
1. Parse args, merge YAML config.
2. Locate the `symbfuzz` BMC binary under `build/`.
3. Call `design_parser.parse_design` to build the `DesignInfo`.
4. Construct the simulator driver via `drivers.make_driver`. Compile the harness unless `--no-compile`.
5. If `--differential-cross-sim`, build a second driver on the alternate backend.
6. Build `CoverageDB`, `BmcInterface`, `CorpusStore` (if `--corpus`), `StateForcer`.
7. Build the `Orchestrator` with all toggles plumbed through.
8. Run `orch.run()`, catch `KeyboardInterrupt` and still emit a partial report.
9. Print campaign summary + native coverage summary + backend name.

---

## 2. `symfuzz/orchestrator.py`

The main loop. Owns the step counters, corpus restore logic, BMC invocation, replay alignment, coverage polling, seed injection, value-class bookkeeping, differential plumbing, and progress logging.

### `class CampaignResult`
Dataclass returned from `run()`: `total_cycles`, `total_states`, `coverage_pct` (tuple-space), `elapsed_sec`, `bmc_invocations`, `bmc_successes`, `terminated_by` (a short reason string).

### `class Orchestrator`

#### `__init__(...)`
Takes references to `DesignInfo`, `SimDriver`, `CoverageDB`, `BmcInterface`, `StateForcer`, `CorpusStore`, plus every campaign knob. Notable state:
- `self.bmc_depth_tiers` — list of `(max_steps, timeout_ms)`; defaults to `[(32, 5000), (64, 15000), (128, 60000)]`.
- `self.seed_stimuli`, `self.seed_cycles` — lever 3 toggles.
- `self.bmc_target_rarity` (lever 4), `self.bmc_corpus_precheck` (lever 5), `self.seed_cross_product` (lever 6), `self.bmc_witness_mutations` (lever 7), `self.bmc_per_reg_budget` (lever 8).
- `self.value_class_coverage`, `self.value_class_target`, `self.value_class_stall`, `self.value_class_bias`, `self.value_class_bias_every`.
- `self._predicate_fn`, `self.cross_driver` — differential hooks.
- `self._sig_buffer: list[bytes]` — rolling buffer of input signatures for value-class attribution.
- `self._reg_attempts: dict[str, int]` — per-register BMC attempt counter for lever 8.

#### `_random_inputs() -> dict[str, int]`
Builds a fresh random input dict for every data input port, each drawn uniformly in `[0, mask]`. The reset port is deasserted (active-low ports get `1`, active-high get `0`).

#### `_load_predicate_module(path)` *(staticmethod)*
Imports a user-supplied Python file via `importlib.util.spec_from_file_location`, requires it to export a `check(state, inputs)` callable, returns it. Raises `RuntimeError` if the file or symbol is missing.

#### `_invoke_predicate(state, inputs, cycle, source)`
Calls the user's `check` function inside a `try`. Return value of `False` or any raised exception is treated as a violation and logged to `violations.jsonl` as `{cycle, source, state, inputs, reason}`.

#### `_record_signature(inputs)`
If value-class coverage is on, computes `signature_for(inputs, data_inputs)` and appends to `self._sig_buffer`. Trims the buffer to its last half when exceeding `self._sig_buf_max`.

#### `_step_and_track(inputs)`
Drives one cycle. Calls `driver.step`, appends inputs to `self.current_path` (the RLE log for corpus writing), records the signature, mirrors to `cross_driver` if set, invokes the predicate callback, returns the new state.

#### `_cross_check_step(inputs, primary_state)`
Applies `inputs` to `self.cross_driver` and compares the returned state to `primary_state`. Diverging register names + their `(primary, secondary)` values are logged to `differential.jsonl`.

#### `_batch_step_and_track(inputs_list)`
Batched variant using `driver.step_trace`. Returns `(states, optional_error)`. Appends each input to `current_path` in order. Records signatures in bulk. On a partial-batch error, only the successfully-executed inputs are appended (equal to `len(states)`).

#### `_bmc_steps_to_cycle_inputs(bmc_steps)`
Walks the raw SMT2-step list from BMC, which alternates `(clk=0, clk=1)`. For every `(clk=0, clk=1)` pair, emits the negedge step's non-clock inputs as a single cycle. This is the **Phase-5 cycle-wrap** that aligns BMC's "step k drives k→k+1" with the simulator's "inputs at posedge drive the transition." A trailing unpaired negedge is dropped.

#### `_batch_step_and_track_raw(inputs_list)`
Same as `_batch_step_and_track` but uses `driver.step_raw_trace` — caller drives the clock. Reserved for diagnostic replay where SMT2-exact semantics are needed.

#### `_corpus_state_matching(target) -> Optional[dict[str, int]]`
*(Lever 5.)* Scans `visited_states` for any state that already satisfies the target (full-equality for plain targets; bit-equality for `{bit, val}` specs). Returns the full state dict for `driver.force()`, or `None`.

#### `_mutate_witness_steps(steps) -> list[dict]`
*(Lever 7.)* Produces a close copy of a BMC witness with one bit flipped in one randomly-chosen input port at one randomly-chosen cycle. Stays near the SAT witness so the target still toggles with high probability.

#### `_extreme_values(width)` *(staticmethod)*
Returns the deduplicated, width-masked extreme set: `{0, 1, mask, MSB, MSB_clear, 0xAA…, 0x55…}`. Used by seed stimuli.

#### `_run_diversification_pass(global_cycle) -> int`
*(Phase 3 weak.)* Picks an under-diversified covered bin via `coverage_db.get_under_diversified_point`, computes up to eight missing signatures with `value_classes.missing_signatures`, generates concrete inputs for each with `value_classes.inputs_for_signature`, applies each for four cycles. Records `source="vc_bias"`. No-op if nothing is under-diversified or all signatures are seen.

#### `_run_seed_stimuli(global_cycle) -> int`
*(Lever 3.)* For each `(data-input port, extreme value)` pair, resets the driver and drives `self.seed_cycles` cycles with that port pinned while other ports draw random. When `self.seed_cross_product` is on, also emits pair-combinations of extremes. Records `source="seed"` (or `"seed_pair"`).

#### `_record(state, cycle, source, contribution=1)`
The single choke-point that inserts a state into `CoverageDB`, prints a verbose `NEW state:` line on discovery, and optionally writes a corpus entry with the current path. Returns `True` if the state is newly observed.

#### `_progress_tick(global_cycle, start_wall, bmc_invocations, bmc_successes)`
Emits a `[prog]` line every `self.log_every` seconds and appends to `progress.jsonl`. Includes the value-class `vc=X/Y(Z.Z%)` suffix when enabled.

#### `_poll_native_coverage(global_cycle, force=False)`
If native coverage is on, calls `driver.collect_coverage()` and folds the snapshot into `CoverageDB.record_coverage_snapshot`. When any new points are observed, attributes the current signature buffer to each new point and clears the buffer. If the corpus is live and new points appeared, the current path is snapshotted as a corpus entry with `source="native"`.

#### `_wire_name_cache()`
Maps register arch-names to their signature widths (reads once, caches). Used by the toggle → BMC target classifier.

#### `_direct_bit(state, sig, bit)` *(staticmethod)*
Extracts one bit from a register value by name. Returns `None` if missing. Part of the direct-signal-probe productivity check.

#### `_toggle_target_to_bmc_target(info) -> Optional[dict]`
Translates a toggle-bin `info` dict into a BMC target:
- Flop-backed signals → `{reg: {bit, val, flip}}` (temporal flip).
- Combinational wires → `{reg: {bit, val}}` (reach-value).
- Unresolved → `None` (falls back to legacy register-value target).

#### `_try_startup_restore() -> bool`
If a corpus with the deepest entry exists and its design hash matches, restores the driver to that entry's state (either by waveform restore or by replaying the RLE input log). Returns whether the restore succeeded.

#### `_mutation_step(global_cycle) -> tuple[int, bool]`
Draws a corpus entry, replays its path, then applies mutation (havoc or splice) for a burst of cycles. Used when `self.mutation_rate > 0` triggers.

#### `run() -> CampaignResult`
The main loop:
1. Open `violations.jsonl` / `differential.jsonl` if their features are on.
2. Try startup restore; else reset + record initial state.
3. If not resumed, optionally run seed stimuli.
4. Enter the main `while` loop:
   - Check global timeout.
   - Replay any pending BMC result (including cycle-wrapping, productivity check, lever-7 witness mutations, lever-2 tier promotion on non-productive).
   - Coverage stall check (tuple/native/any/value_class).
   - Periodic diversification bias pass (Phase 3 weak).
   - If stalled and coverage targets enabled: pick target via `CoverageDB.get_uncovered_coverage_target(rarity_weighted=…)`, apply lever-8 per-reg budget check, optionally lever-5 corpus pre-check, then call BMC with the current tier's `(max_steps, timeout_ms)`. On None, promote or blacklist.
   - Mutation step.
   - Batched random step.
   - Coverage poll, progress tick.
5. Return `CampaignResult`.

---

## 3. `symfuzz/coverage_db.py`

Sqlite-backed coverage and state tracking.

### `_state_hash(state)` / `_target_key(target)`
Canonical hashing: `_state_hash` uses a stable JSON dump + SHA1; `_target_key` builds a frozenset of `(reg, val)` pairs so equivalent targets collide.

### `class CoverageDB`

#### `__init__(db_path, design)`
Opens/creates the sqlite file. Initialises all tables via `_init_schema`. Runtime-only dicts: `_exhausted_targets`, `_exhausted_coverage_points`, `_point_tier`.

#### `_init_schema()`
Creates the following tables idempotently:
- `visited_states(state_hash PK, state_json, first_seen, cycle_count)`
- `coverage_log(cycle, new_states, total_states)`
- `coverage_points(point_id PK, backend, kind, first_seen_cycle, first_seen_time)`
- `coverage_point_universe(point_id PK, kind, details_json)`
- `value_class_hits(point_id, signature, first_seen_cycle, PK(point_id, signature))`
- `corpus_entries(state_hash PK, segments_json, total_depth, source, wdb_path, hit_count, created, contribution_score)`

Idempotent column migrations for older DBs.

#### `record_state(state, cycle) -> bool`
Inserts the state (if novel), appends to `coverage_log`. Returns True on first insertion. Updates stall timestamps.

#### `is_visited(state)`, `total_states()`
Simple queries.

#### `stale_since(source)`
Cycles since the last *new coverage event*. `source` is one of `"tuple"`, `"native"`, `"any"`, `"value_class"`. (The value-class branch is handled in the orchestrator; this method returns the cycle delta since the last tuple/native event.)

#### `mark_target_exhausted(target)`
Adds `_target_key(target)` to `_exhausted_targets` so future neighbour-picks skip it.

#### `get_unvisited_neighbor_target()`
Picks a register-value target that has NOT yet appeared in any visited state. Round-robins registers across calls. Used as the fallback when no coverage-target is available.

#### `record_coverage_snapshot(snapshot, cycle, backend) -> int`
Upserts the snapshot's covered points into `coverage_points` and the universe into `coverage_point_universe`. Returns the count of newly-covered points and stashes their IDs on `self.last_new_point_ids` for value-class attribution.

#### `record_value_class_hit(point_id, signature, cycle) -> bool`
Inserts a `(point_id, signature)` pair into `value_class_hits`. Returns True if newly inserted, False on conflict. Does not commit — caller batches commits.

#### `value_class_diversification(target_n) -> (diversified, total)`
Counts how many covered points have ≥ `target_n` distinct signatures. Total is the coverage universe size.

#### `get_under_diversified_point(target_n) -> (pid, seen_signatures) | None`
Returns a covered point whose signature count is below `target_n`. Prefers bins with the most existing signatures (closest to diversified) so the next bias pass pushes the metric.

#### `value_class_per_point_counts()`
Returns `(point_id, distinct_signature_count)` sorted ascending.

#### `promote_coverage_point(point_id, max_tier) -> bool`
*(Lever 2.)* Bumps `_point_tier[point_id]` by 1. Returns True if the new tier is ≤ `max_tier`; False if promotion would overflow (caller should blacklist).

#### `get_point_tier(point_id) -> int`
Reads the current tier (default 0).

#### `mark_coverage_point_exhausted(point_id)`
Adds `point_id` to `_exhausted_coverage_points`, permanently excluding it from target selection.

#### `get_uncovered_coverage_target(rarity_weighted=False) -> dict | None`
The core target-picker. Builds a candidate list from `coverage_point_universe \ coverage_points`, excluding exhausted points. Sorts by tier ascending, then (if rarity-weighted) by negative sibling-coverage count, then random. Returns the winner with its kind, details, `point_id`, and `tier` attached.

#### `native_covered()`, `is_native_covered(pid)`, `is_native_complete()`, `native_covered_by_kind()`, `native_coverage_pct()`, `native_summary()`
Aggregate queries for the progress line and the final report.

#### `close()`
Commits and closes the connection.

---

## 4. `symfuzz/bmc_interface.py`

Thin subprocess wrapper around the `build/symbfuzz` binary.

### `class BmcResult`
Dataclass: `depth`, `steps` (list of per-step input dicts), `target`.

### `class BmcInterface`

#### `__init__(binary, design, max_steps, timeout_ms, verbose)`
Stores defaults and accumulator stats (`last_duration_ms`, `total_duration_ms`, `total_calls`). Opens no log file by default.

#### `open_log(path)` / `close_log()` / `_log_record(rec)`
Appends one JSON-per-line record per BMC call to `bmc.jsonl`. Safe to call without opening (no-ops if no fh).

#### `find_sequence(targets, kind, max_steps=None, timeout_ms=None) -> Optional[BmcResult]`
Builds the `symbfuzz` command line:
- `--top`, `--output json`.
- `--max-steps`/`--timeout` from effective overrides (per-tier).
- For each target entry: `--target`, `--target-bit`, or `--target-flip` depending on the spec.

Spawns the subprocess with `subprocess.run`. The subprocess timeout is `eff_timeout_ms/1000 + max(2, min(10, 20% of that))` to give the binary enough grace without dominating short budgets.

Return:
- Exit 0 → SAT; parse JSON on stdout for `depth` and `steps`. Returns the `BmcResult`.
- Exit 2 → UNSAT; returns `None`.
- Timeout → `None`.
- Other non-zero → logged, returns `None`.

All outcomes are recorded to the log file.

#### `_record_timing(t0, tag, targets)`
Bookkeeping for timeout/error paths.

---

## 5. `symfuzz/design_parser.py`

Turns Verilog source into a `DesignInfo` the orchestrator can use.

### `class PortInfo`
Per-input metadata. `name`, `width`, `direction`. `mask`, `verilator_ctype` derived.

### `class RegisterInfo`
Per-register metadata. `arch_name` (human name), `mangled_name` (from Yosys), `width`. Derived: `xsim_path` (`module_name/register_path` for xsim `get_value`), `verilator_path` (Verilator-mangled C++ field name).

### `class DesignInfo`
Holds ports, registers, module name, clock/reset names, Verilog paths, optional sv2v cache path. `data_inputs` property filters ports to exclude clock/reset. `max_state_space` is the product of `2**width` over registers.

### `_find_sv2v() -> str | None`
Searches `$PATH` for the `sv2v` binary.

### `_sv2v_convert(files, top_module, out_path) -> list[str]`
Runs `sv2v` on the input files, writes the flattened Verilog-2005 output to `out_path`. Returns a single-element list containing `out_path`.

### `parse_design(verilog_paths, top, sv2v) -> DesignInfo`
Main entry point. Optionally preprocesses with sv2v, invokes Yosys to emit SMT2 annotations, parses those annotations to extract ports and registers, returns a populated `DesignInfo`.

### `_parse_smt2_annotations(smt2_text, verilog_path) -> DesignInfo`
Walks Yosys's SMT2 output looking for `; yosys-smt2-input` / `-output` / `-register` comments, plus the Boolean-width annotations. Builds the typed metadata.

### `_extract_arch_name(mangled)` / `_build_bool_map(smt2_text, module)`
Internal helpers for de-mangling Yosys names and finding Boolean-typed (width-1) registers.

---

## 6. `symfuzz/sim_driver.py` + `symfuzz/drivers/`

### `symfuzz/sim_driver.py`
Exposes `SimDriver` (deprecated alias of `BaseStdioDriver`), `make_driver(backend, …)` factory, and the `CoverageSnapshot` dataclass. Used by `cli.py` to instantiate the right backend.

### `symfuzz/drivers/base.py`

#### `class CoverageSnapshot`
Immutable: `points` (set of covered point IDs), `universe` (set of all instrumented point IDs), `kinds` (per-ID kind string), `details` (per-ID dict with reg/bit/dir/file/line), `total`.

#### `class BaseStdioDriver`
Shared infrastructure for the JSON-over-stdio protocol.

- `__init__(design, tb_dir, verbose)` — holds paths, no subprocess yet.
- `compile()` — abstract; each backend compiles its harness.
- `load()` — spawns the harness subprocess.
- `close()` — sends `quit`, waits, reaps.
- `_send(obj)` / `_recv()` — one-request-one-response JSON primitives.
- `step(inputs)` / `step_trace(inputs_list)` — full-cycle commands.
- `step_raw(inputs)` / `step_raw_trace(inputs_list)` — SMT2-semantic commands (caller drives the clock).
- `reset(cycles)` — assert reset for N cycles, then deassert and run one more edge.
- `read_state()` — snapshot registers.
- `force(state)` — inject register values.
- `checkpoint(path)` / `restore(path)` — waveform snapshot; Verilator only.
- `collect_coverage()` — returns a `CoverageSnapshot` or `None`.
- `random_step(deassert_reset=True)` — convenience for deterministic random walks.

### `symfuzz/drivers/verilator.py`
Concrete Verilator backend. Renders the C++ harness template, invokes Verilator to build a binary, spawns it and speaks JSON to it. Native coverage is parsed from `verilator_cov.dat` by `_parse_verilator_cov_dat`, which de-duplicates toggle bits into one `xtgl:HIER:SIG:BIT:DIR` point per direction and attaches details `{reg, bit, dir, file, line}`.

### `symfuzz/drivers/xsim.py`
Concrete xsim backend. Renders the TCL bridge template, invokes `xvlog`/`xelab`/`xsim -R` to launch. Native coverage goes through `write_xsim_coverage` + `xcrg` HTML parse.

---

## 7. `symfuzz/corpus_store.py`

Persistent corpus: each novel state gets a record `(state_hash, segments, total_depth, source, optional_waveform)`.

### `class CorpusEntry`
Dataclass; one row of `corpus_entries`.

### `append_input(segments, inputs)`
RLE-extends a segments list in place with one more cycle of `inputs`. Merges with the previous segment if the dict is identical.

### `class CorpusStore`

- `__init__(db_path, design, wdb_dir, wdb_budget_mb, verbose)` — opens sqlite + bootstraps the waveform cache directory with a design-hash sanity check.
- `add_entry(state_hash, segments, source, driver, contribution)` — insert or update. Optionally takes a waveform snapshot.
- `restore(state_hash, driver) -> bool` — driver-appropriate restore (waveform if supported; otherwise replay the RLE log from reset).
- `has(state_hash)` — membership test.
- `sample(rng, uniform)` — weighted or uniform sampling of entries for mutation.
- `pick_deepest()` — entry with the largest `total_depth`.
- `close()`, `_fetch(state_hash)` — housekeeping.
- `_should_write_wdb(total_depth)`, `_wdb_path_for(state_hash)`, `_try_wdb_restore(entry, driver)`, `_verify_and_evict_on_mismatch(...)`, `_evict_row(...)`, `_evict_lru_wdb()` — waveform cache management.
- `_compute_design_hash(snapshot_file)`, `_check_meta_and_invalidate_wdbs_if_stale()` — invalidation against a changed design.

---

## 8. `symfuzz/state_forcing.py`

Uses BMC to compute an input sequence, then replays it with `driver.force` pre-applied so internal registers start at chosen values. Primarily used by the orchestrator when the BMC witness is known to require specific non-zero initial register contents.

---

## 9. `symfuzz/mutations.py`

Input-stream mutations over a list of RLE segments.

- `havoc(segments, rng)` — flips random bits in random segments' inputs.
- `splice(segments_a, segments_b, rng)` — concatenates a head of A with a tail of B.

Both return a fresh segment list; neither mutates in place.

---

## 10. `symfuzz/value_classes.py`

Value-class coverage helpers.

- Class indices `VC_ZERO=0 … VC_MID=8` and a matching `CLASS_LABELS` list.
- `classify(val, width) -> int` — deterministic O(1) classification.
- `signature_for(inputs, port_order) -> bytes` — canonical bytes with one class index per port.
- `representative_value(cls_idx, width, rng) -> int` — a concrete value in the named class; buckets with multiple representatives (small, pow2, alt, mid) draw via `rng`.
- `inputs_for_signature(sig, port_order, rng) -> dict` — maps a signature back to one concrete input dict.
- `missing_signatures(seen, n_ports, limit) -> list[bytes]` — produces up to `limit` signatures not in `seen` by single-class perturbations of seed signatures.
- `decode_signature(sig, port_order) -> dict[str, str]` — human-readable rendering.

---

## 11. `symfuzz/testbench_gen.py`

Emits a UVM scaffold (`tb_top.sv`, agent/driver/monitor/sequence/environment, packages) from the templates in `symfuzz/templates/uvm_*.sv.j2`. The scaffold is independent of the campaign loop; users can drop it into any UVM-capable simulator to replay corpus paths.

---

## 12. `symfuzz/templates/`

Jinja2 templates. These are rendered once per campaign into the output directory.

- `verilator_harness.cpp.j2` — C++ REPL that calls the Verilator model each step. Handles the full JSON protocol (including `step_raw*`, `coverage` via `VerilatedCov::write`).
- `verilator_compile.sh.j2` — thin shell wrapper that runs `verilator` with the right flags (threading, coverage, SV flags, `--binary`).
- `tcl_bridge.tcl.j2` — equivalent REPL for xsim. Uses `get_value`/`set_value` for state and inputs, `run 1ns` to clock, `write_xsim_coverage` for coverage dumps.
- `compile.sh.j2` — xsim compile helper (`xvlog` → `xelab`).
- `uvm_*.sv.j2` — UVM package emitting glue. Each maps one-to-one to a UVM component.

---

## 13. `src/` — the C++ BMC binary

The binary is built out-of-tree under `build/` via `cmake`/`make`.

### `src/main.cpp`
CLI entry:
- Parses `--top`, `--max-steps`, `--timeout`, `--output`, `--verbose`.
- Collects one or more `--target`, `--target-bit`, `--target-flip` specifications.
- Calls into `frontend/` to elaborate the design via Yosys + sv2v (if `--sv2v`).
- Calls into `bmc/` to unroll `max_steps` steps and ask the solver.
- Emits SAT as JSON (`{"depth": N, "steps": [{port: val, …}, …]}`) on stdout and exits 0; UNSAT exits 2; timeout/error exits non-zero with a message on stderr.

### `src/frontend/`
Handles design elaboration: runs Yosys, applies `clk2fflogic` to turn the netlist into a flat SMT2 transition system, dumps to `frontend.smt2`. Parses Yosys's annotations to discover ports, registers, widths, and clock name. The Python side consumes the same annotations via `design_parser.py` for cross-process agreement.

### `src/solver/`
Z3 wrapper. Builds the transition relation from the SMT2 file, unrolls K steps into variables `port@k`, `reg@k`, `next_reg@k`, and asserts the user's target constraints against the final step (or, for `--target-flip`, across step K and K−1). Asks Z3 for a model; emits the per-step input values if SAT.

### `src/bmc/`
High-level unrolling loop and target-assertion composer. Owns the depth iteration: starts at 1, grows to `max_steps`, returns the shallowest SAT depth.

### `src/utils/`
Generic helpers: JSON emission, logging, CLI option parsing.

---

## 14. Data exchange formats

### 14.1 Simulator JSON protocol

Every command is one line of JSON on stdin, every response one line on stdout. See [ARCHITECTURE.md §10](ARCHITECTURE.md) for the full command list.

- Request shapes:
  - `{"cmd":"step","inputs":{"port":val,...}}`
  - `{"cmd":"step_trace","inputs":[{...},{...}]}`
  - `{"cmd":"step_raw","inputs":{...}}` — caller drives `clk`.
  - `{"cmd":"step_raw_trace","inputs":[...]}`
  - `{"cmd":"reset","cycles":N}`
  - `{"cmd":"read_state"}`
  - `{"cmd":"force","state":{"reg":val,...}}`
  - `{"cmd":"coverage"}`
  - `{"cmd":"checkpoint","path":"…"}` / `{"cmd":"restore","path":"…"}`
  - `{"cmd":"quit"}`
- Response shapes:
  - `{"state":{"reg":val,...}}` or `{"states":[...]}`
  - `{"ok":true}`
  - `{"error":"…"}`
  - `{"report":"<native-cov text>"}` (from `coverage`)

### 14.2 BMC binary JSON output

```json
{
  "depth": 8,
  "steps": [
    {"clk": 0, "rst_n": 1, "op_a_i": 0, ...},
    {"clk": 1, "rst_n": 1, "op_a_i": 0, ...},
    ...
  ]
}
```

Step count is `2 × depth + 1` because `clk2fflogic` uses half-cycle transitions.

### 14.3 Progress JSON (`progress.jsonl`)

One record per `[prog]` tick. Fields include `t`, `cycle`, `cps`, `states`, `native_covered`, `native_total`, `corpus_size`, `bmc_invocations`, `bmc_successes`, `bmc_avg_s`, `mutations`, `poll_ms`, plus `vc_diversified` / `vc_total` when value-class coverage is on.

### 14.4 BMC JSON (`bmc.jsonl`)

One record per BMC call: `{t, outcome, target, kind, duration_ms, depth, rc?}` plus (on SAT after replay) `{target_hit, direct_hit, productive, native_delta, states_delta}`.

### 14.5 Differential logs

- `violations.jsonl` — per predicate failure: `{cycle, source, state, inputs, reason}`.
- `differential.jsonl` — per cross-sim divergence: `{cycle, inputs, diffs}`.

---

## 15. Testing hooks

### 15.1 The `force` path
`driver.force()` is the only way to reach "otherwise unreachable" states outside a BMC witness. `state_forcing.py` wraps it for disciplined use; bare `driver.force()` is available for ad-hoc tests.

### 15.2 The `step_raw` path
Intended for testing the SMT2-semantic replay (e.g. verifying a BMC model change). The orchestrator's main loop uses cycle-wrapped `step_trace` in almost all cases; `step_raw` only runs when a caller explicitly opts in.

### 15.3 Mutation-testing backend (planned)
An RTL mutator + parallel driver array is not yet shipped. When added, it will hook between `driver.compile()` and `driver.load()` in `cli.py` and mirror each `step` through the mutant drivers from `Orchestrator.run()`.

---

See [USER_MANUAL.md](USER_MANUAL.md) for how these pieces are used in practice.
