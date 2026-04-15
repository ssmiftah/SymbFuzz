# SymbFuzz — Architecture

This document describes SymbFuzz at a design level: the major components, how they communicate, the data flow during a campaign, and the abstractions that let the same orchestrator drive multiple simulation backends and BMC strategies.

For implementation detail (per-function behaviour, data structures), see [TECHNICAL.md](TECHNICAL.md). For how to actually run the tool, see [USER_MANUAL.md](USER_MANUAL.md).

---

## 1. The big picture

SymbFuzz is a **coverage-directed fuzzer** with a **BMC back-end**. The top-level loop looks like this:

```
 ┌──────────────────────────────────────────────────────────────┐
 │                       Orchestrator (Python)                  │
 │                                                              │
 │   ┌────────────┐  stimulus   ┌─────────────┐   states        │
 │   │  mutation  │────────────▶│  SimDriver  │──────────────┐  │
 │   │  + seed    │             │ (verilator  │              │  │
 │   │  + BMC     │◀────────────│  or xsim)   │              │  │
 │   │  replay    │   coverage  └─────────────┘              │  │
 │   └────────────┘                                          ▼  │
 │         ▲                                       ┌─────────────┐
 │         │ SAT + witness trace                   │  CoverageDB │
 │         │                                       │  (sqlite)   │
 │   ┌─────┴───────┐  target {reg: val}            └─────────────┘
 │   │ BmcInterface│◀──────────── orchestrator picks uncovered
 │   │ (subprocess)│               bin / register state to reach
 │   └─────────────┘                                            │
 │         │                                                    │
 │         ▼                                                    │
 │   ┌─────────────┐                                            │
 │   │ symbfuzz    │   Yosys clk2fflogic → SMT2 → Z3            │
 │   │ (C++ BMC)   │                                            │
 │   └─────────────┘                                            │
 └──────────────────────────────────────────────────────────────┘
```

There are two processes per campaign:
- The **Python orchestrator** runs the main loop, owns all state, and spawns the BMC binary on demand.
- The **C++ BMC binary** (`build/symbfuzz`) is invoked fresh for each target; it elaborates the design once per invocation, unrolls, and asks Z3 to find a witness.

Inside the orchestrator process:
- **SimDriver** is the abstract I/O to the simulator (Verilator REPL over pipes, or xsim TCL bridge). The orchestrator never knows which backend it's talking to.
- **CoverageDB** is a sqlite database holding visited states, covered coverage points, and (optionally) value-class signatures.
- **CorpusStore** is a second sqlite table + optional Verilator waveform-database files, used for warm-restart replay.

---

## 2. Module inventory

### Python (`symfuzz/`)

| Module | Role |
|--------|------|
| `cli.py` | Argument parsing, YAML config merge, wiring the object graph, final report. |
| `orchestrator.py` | The main loop. Picks targets, drives the simulator, calls BMC, records coverage, runs mutations and seeds. |
| `design_parser.py` | Parses the Verilog via Yosys, extracts top-level ports, registers, clock/reset names, signal widths. Optional sv2v preprocessing. |
| `sim_driver.py` / `drivers/` | Abstract driver base class + concrete Verilator and xsim implementations. |
| `coverage_db.py` | Sqlite schema: visited states, coverage universe, coverage-point exhaustion, value-class hits. |
| `corpus_store.py` | Corpus persistence with RLE input logs and optional waveform checkpoints. |
| `bmc_interface.py` | Python-side subprocess wrapper around the C++ BMC binary. |
| `state_forcing.py` | Helper that uses BMC + the driver's `force` command to pin internal registers. |
| `mutations.py` | Havoc- and splice-style input mutation operators. |
| `value_classes.py` | Classifier, signature generator, and missing-signature helpers for value-class coverage. |
| `testbench_gen.py` | Emits a UVM scaffold for offline reuse of the corpus. |
| `templates/` | Jinja2 templates for the Verilator C++ harness, the xsim TCL bridge, and UVM glue. |

### C++ (`src/` + `include/`)

| Module | Role |
|--------|------|
| `frontend/` | Yosys invocation, sv2v normalisation, SMT2 emission. |
| `solver/` | Z3 wrapper, SMT assertion builders for the various target kinds. |
| `bmc/` | Unrolling engine, target assertion composer. |
| `main.cpp` | CLI entry point; parses `--target`, `--target-bit`, `--target-flip`, emits JSON on stdout. |
| `utils/` | Shared helpers (JSON, logging). |

---

## 3. The campaign lifecycle

1. **Design parse.** `design_parser.DesignInfo` is built from the Verilog sources. This reports top-level ports (with widths), registers, clock/reset names. If `--sv2v` is on, the sources are normalised first.
2. **Testbench generation.** Jinja2 expands the simulator-specific harness:
   - For Verilator: a C++ REPL harness (`verilator_harness.cpp`) is emitted and compiled.
   - For xsim: a TCL bridge (`tcl_bridge.tcl`) is emitted.
   In both cases the harness exposes the same line-delimited JSON protocol (`step`, `step_trace`, `step_raw*`, `reset`, `read_state`, `force`, `coverage`, `quit`).
3. **Driver start.** The harness is spawned as a child process; the driver speaks JSON over stdio.
4. **Optional seed injection.** If `--seed-stimuli` is on, the orchestrator applies pinned-port extreme stimuli to the driver before the main loop (see §6).
5. **Main loop.**
   - Random or mutated inputs are batched and applied through the driver.
   - States returned by the driver are recorded in `CoverageDB`.
   - If native coverage is enabled, a periodic `coverage` command polls the driver, and new points are recorded.
   - When a *coverage-stall* threshold is reached, the orchestrator picks an uncovered bin (or register state), asks `BmcInterface` to solve for a witness, and replays the witness through the driver.
   - SAT paths that fail to produce new coverage are retired; survivors advance the campaign state.
6. **Termination.** The loop exits on timeout, full coverage, an explicit `no-more-targets` signal, or a configurable unproductive-BMC streak limit.
7. **Reporting.** The final campaign result, coverage snapshot, and progress log are written to the output directory.

---

## 4. Data flow

### 4.1 Inputs → states

The design has a fixed set of **data input ports** (everything except clock and reset). Every cycle, the orchestrator builds a dict `{port_name: value}`, hands it to the driver via `step` or `step_trace`, and receives the resulting **register state** dict `{arch_name: value}`.

Random inputs draw uniformly from each port's width. Mutation inputs take an existing corpus entry and flip bits or splice segments. Seeds pin one (or more) ports at extreme values while other ports draw random.

### 4.2 States → corpus + coverage

Each new state is:
- Hashed and inserted into `visited_states`.
- Optionally appended to the corpus (with the path of inputs that produced it, in RLE form).
- Used to update coverage-stall timers.

If native coverage is on, the driver also emits coverage snapshots on demand; these are folded into the `coverage_points` and `coverage_point_universe` tables.

### 4.3 Coverage holes → BMC targets

When the coverage metric plateaus for `stall_cycles`, the orchestrator picks the next target:

1. If `--coverage-target` is on (default), call `coverage_db.get_uncovered_coverage_target()` to fetch an uncovered native point, preferring toggle bins (and optionally rarity-weighted).
2. Translate the bin to a BMC target dict:
   - Flop-backed toggles → `{reg: {bit, val, flip}}` (temporal flip goal).
   - Combinational wires → `{reg: {bit, val}}` (reach-value goal).
3. Call `BmcInterface.find_sequence(target)` with a depth/timeout budget drawn from the tier table (see §5).

If BMC returns SAT, the orchestrator replays the witness through the driver using **cycle-wrapped** inputs (see §7). If the replay produces new coverage, the call was *productive*; otherwise the bin is retired or promoted to a deeper BMC tier.

### 4.4 Witness → replay

A BMC witness is a list of SMT2 step inputs. The orchestrator:
1. Forces the simulator to a known zero-state (matching BMC's `assume_zero_init`).
2. Pairs the witness's `(clk=0, clk=1)` steps into cycles, taking the *negedge* step's non-clock inputs (cycle-wrapping).
3. Applies each cycle via `step_trace` in one batched call; the simulator drives one `clk_posedge` per cycle.
4. Polls coverage and checks whether the targeted bin actually toggled.

---

## 5. Target scheduling: the BMC tier machine

Targets aren't just "fire BMC and hope." Each uncovered coverage point carries a **tier** (0 by default). Each tier has a `(max_steps, timeout_ms)` budget:

- **Tier 0** is cheap and shallow (e.g. 32 steps / 5 s). Drains the easy bins fast and blacklists quickly-proven-unreachable ones.
- **Tier 1** escalates (64 steps / 15 s). Only bins that timed out or UNSAT'd at tier 0 reach here.
- **Tier 2** is deep (128 steps / 60 s). Reserved for bins requiring long witness traces.

On BMC None (UNSAT/timeout), the point's tier is incremented. If the point is already at the top tier, it's **blacklisted** — `coverage_db.mark_coverage_point_exhausted()` keeps it out of future target selection.

The target-selection query sorts candidates by tier ascending, so the orchestrator drains the tier-0 pool first before escalating anything. This makes short campaigns productive and reserves deep-solver time for genuine corner cases.

---

## 6. Seed stimuli

Before the main loop, the orchestrator can inject **structured seed bursts** whose role is to toggle datapath bits that random exploration would miss:

- **Pinned-port seeds** (`--seed-stimuli`): for each data input and each of nine value classes (zero, one, small, MSB, neg1, max, pow2, alt, mid), hold that port at a representative value for `seed_cycles` cycles while all other ports draw random. This deterministically exercises every high-order bit of every datapath register without a single BMC call.
- **Cross-product seeds** (`--seed-cross-product`): additionally emits seeds pinning **two** ports simultaneously, capturing coordinated-extreme patterns (e.g. `op_a=INT_MIN ∧ opcode=DIV_SIGNED`).

Seeds are applied once; their states populate the corpus and immediately advance native coverage before BMC is invoked.

---

## 7. Replay alignment (cycle-wrapping)

The BMC model uses `clk2fflogic` semantics: *the inputs at step k drive the transition to step k+1*. The simulator uses `always_ff @(posedge clk)`: *the inputs at the posedge drive the transition*. These differ by one SMT step.

A BMC trace alternates `(clk=0, clk=1)`. To replay correctly, SymbFuzz walks a two-wide window and, for each `(clk=0, clk=1)` pair, emits **one cycle** that drives the simulator with the negedge step's non-clock inputs. One posedge per pair, inputs latched *before* the edge. This is the Phase-5 fix that made BMC replays actually exercise the targeted bins rather than merely arrive at a different state.

A diagnostic *raw* replay path (`step_raw`, `step_raw_trace`) remains, matching SMT2 transition semantics exactly for debugging or for backends that might in future follow them.

---

## 8. Value-class coverage (optional)

The primary coverage metric is binary: a toggle bin is either covered or not. The **value-class coverage** extension refines this: each bin can be hit under many **signatures** — a tuple of value-class indices, one per data input port, classifying the inputs that were active when the bin was last discovered.

Three phases are optional and independently toggleable:

- **Phase 1** — signature capture. Inputs seen in each coverage-poll window are attributed to any bin that newly covered during that window. Results are accumulated in a `value_class_hits` table and reported as `vc=diversified/total` on the progress line.
- **Phase 2** — value-class stall. Instead of stalling on "no new toggle bins," the orchestrator stalls on "no new diversified bins," changing BMC target pressure.
- **Phase 3 (weak)** — diversification bias pass. Periodically, the orchestrator picks an under-diversified bin, computes a few missing signatures, and injects short seed bursts whose inputs classify into those missing signatures. This nudges diversification without needing BMC to understand value classes.

For security fuzzing, value-class coverage gives a more honest answer to "did we test this code under varied inputs?" than bit-toggle coverage alone.

---

## 9. Differential testing (optional)

Three complementary modes, independently toggleable:

- **Predicate module** (`--predicate-module file.py`). The user supplies a Python function `check(state, inputs)`. Every cycle, violations (returning `False` or raising) are logged to `violations.jsonl`. Best for spec-level correctness (e.g. "`out_vld` never during reset").
- **Cross-simulator** (`--differential-cross-sim`). The orchestrator spawns a second driver on the other backend (Verilator↔xsim) and mirrors every `step` to both. State-hash divergence is logged to `differential.jsonl`. Hunts simulator tool bugs and pipeline/compilation inconsistencies.
- **Mutation testing** *(planned, not shipped)*. RTL mutator produces N faulty copies; the fuzzer runs them in lockstep with the original and scores which mutants are detected. Measures "would my campaign catch planted bugs of this class?"

None requires a reference model, which makes them practical for security research on designs where a golden model doesn't exist.

---

## 10. The SimDriver abstraction

Every backend exposes the same JSON-over-stdio protocol:

| Command | Purpose |
|---------|---------|
| `step` | One cycle: latch inputs, pulse clock, return state. |
| `step_trace` | Batched `step`; returns a list of states. |
| `step_raw` / `step_raw_trace` | Like `step*` but caller drives the clock directly (used for BMC diagnostic replay). |
| `reset` | Assert reset for N cycles. |
| `read_state` | Snapshot current registers. |
| `force` | Write arbitrary values into tracked registers. |
| `coverage` | Dump the simulator's native coverage report. |
| `checkpoint` / `restore` | Waveform snapshot (Verilator only). |
| `quit` | Clean shutdown. |

All commands return one JSON line on stdout. Errors never crash the harness — they appear as an `"error"` field in the response.

The Python side (`drivers/base.py`) implements this as `SimDriver`; subclasses (`VerilatorDriver`, `XsimDriver`) are thin wrappers that own the subprocess, the write/read pipes, and backend-specific compilation.

---

## 11. Concurrency

One orchestrator, one driver process, one BMC subprocess (spawned per call). The orchestrator is **single-threaded Python**. BMC calls are blocking subprocesses; during a BMC call, no simulation happens.

For multi-arm ablation experiments, the tool does **not** run multiple campaigns in one process. Instead, `run_armH_parallel.sh` launches independent SymbFuzz invocations pinned to disjoint CPU blocks via `taskset`. Each campaign owns its own output directory, database, and driver — no shared state.

---

## 12. Extension points

The following places are designed to be extended:

- **New simulator backend** — subclass `SimDriver`, implement the JSON command set, register in `drivers/__init__.py:make_driver`.
- **New BMC target kind** — add a handler in `src/main.cpp`'s arg parser and the corresponding SMT clause builder in `src/bmc/`. Extend `BmcInterface.find_sequence` to emit the new flag.
- **Custom value classes** — edit `symfuzz/value_classes.py:classify` and `CLASS_LABELS`. Signatures remain compact bytes.
- **Custom mutation operators** — add functions in `symfuzz/mutations.py` and wire into `orchestrator._mutation_step`.
- **Coverage-target selection heuristics** — extend `coverage_db.get_uncovered_coverage_target` with new ordering clauses; the rarity-weighted variant is a template for this.

---

## 13. Non-goals

- **Multi-clock domains.** Every cycle is a single clock edge.
- **Spec discovery / assertion synthesis.** The differential modes consume predicates; they don't discover them.
- **Cross-process shared corpus.** Parallel campaigns run independent corpora; merging is out of scope (easy as a post-process).
- **Online tool updates.** The BMC binary is spawned per target; there's no persistent solver state across calls.

---

See [TECHNICAL.md](TECHNICAL.md) for the function-level view of everything above.
