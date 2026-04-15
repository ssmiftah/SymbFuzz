# SymbFuzz — User Manual

This manual covers installing SymbFuzz, running a campaign, writing a config file, interpreting the output, and using the optional features (tiered BMC, seed stimuli, value-class coverage, differential testing, parallel ablations).

For the design-level overview see [ARCHITECTURE.md](ARCHITECTURE.md). For per-function internals see [TECHNICAL.md](TECHNICAL.md).

---

## 1. Installation

### Required

| Dependency | Purpose |
|------------|---------|
| Python ≥ 3.10 | Orchestrator, CLI |
| Yosys ≥ 0.40 | BMC front-end (via the C++ binary) |
| Z3 ≥ 4.12 | SMT solver linked by the C++ binary |
| A C++17 compiler | Building the `symbfuzz` binary |
| `cmake` + `make` | Build system for the binary |

### At least one simulator

| Backend | Install |
|---------|---------|
| Verilator ≥ 5.020 | `apt install verilator`, or build from source |
| Xilinx Vivado xsim | Vivado 2024.x+ (licensed; xsim ships with the standard edition) |

### Optional

| Tool | When |
|------|------|
| `sv2v` | If your sources use SystemVerilog constructs Yosys can't parse (see §8) |
| `xcrg` | Required for xsim native-coverage report parsing (bundled with Vivado) |

### Build steps

```bash
# 1. Clone the repository
git clone <repo-url> SymbFuzz
cd SymbFuzz

# 2. Build the C++ BMC binary
mkdir -p build && cd build
cmake ..
make -j
cd ..
# The binary is now at build/symbfuzz — leave it there; the CLI looks for it.

# 3. Python package (editable install recommended during development)
python -m venv .venv
source .venv/bin/activate
pip install -e .

# 4. Verify
symfuzz --help
```

---

## 2. Running your first campaign

A minimal invocation needs only the top module name and the Verilog source(s):

```bash
symfuzz --top counter --verilog path/to/counter.v \
        --sim verilator \
        --native-coverage \
        --timeout 60 \
        --output-dir counter_out
```

What happens:
1. SymbFuzz runs Yosys on your source to learn the design's ports and registers.
2. It renders and compiles the Verilator harness under `counter_out/verilator/`.
3. It starts a campaign for 60 seconds, driving random (and, if enabled, seeded) stimuli.
4. On coverage stalls it spawns the BMC binary to find witnesses for uncovered coverage bins.
5. At exit, it writes `counter_out/progress.jsonl`, `counter_out/coverage.db`, and a printed summary.

To see what's happening, add `--verbose`.

---

## 3. Using a YAML config

For anything beyond the quickstart, prefer a config file. Every CLI flag has a matching YAML key (replace hyphens with underscores). Precedence is:

```
argparse defaults  <  YAML values  <  explicit --flag on the command line
```

Example `my_campaign.yaml`:

```yaml
top: counter
verilog:
  - examples/counter.v

sim:              verilator
native_coverage:  true
coverage_poll:    20
stall_source:     native
stall_cycles:     500
timeout:          14400       # 4 hours
verilator_threads: 4
output_dir:       counter_run

# All lever toggles default-off; turn on what you want.
seed_stimuli:     true
seed_cycles:      80

bmc_depth_tiers:  "32:5000,64:15000,128:60000"
bmc_unproductive_limit: 10

value_class_coverage: true
value_class_target:   8

verbose:          true
log_every:        60
```

Run:

```bash
symfuzz --config my_campaign.yaml
```

CLI flags can still override anything in the YAML — useful for quick ablations:

```bash
symfuzz --config my_campaign.yaml --timeout 600 --bmc-target-rarity
```

---

## 4. Flag reference

Grouped by purpose. For *every* flag, the YAML key is the same with hyphens replaced by underscores.

### 4.1 Design & build

| Flag | Default | What |
|------|---------|------|
| `--top NAME` | — (required) | Top-level module. |
| `verilog ...` (positional) | — | Source files, dependencies first. Can be given via `verilog:` in YAML. |
| `--sim {verilator,xsim}` | `xsim` | Simulation backend. |
| `--sv2v` | off | Preprocess sources with `sv2v` before Yosys. |
| `--no-uvm` | off | Skip UVM testbench scaffold emission. |
| `--no-compile` | off | Assume an existing compiled harness; skip `verilator`/`xvlog`+`xelab`. |
| `--gen-only` | off | Emit harness/TB only and exit without running. |
| `--verilator-threads N` | `1` | Verilator `--threads N`. |
| `--clock-period STR` | `"1ns"` | Time step used by the xsim `run` command. |

### 4.2 Output

| Flag | Default | What |
|------|---------|------|
| `--output-dir DIR` | `<top>_symfuzz/` | Where artefacts go. Created if absent. |
| `--progress-log PATH` | `<output-dir>/progress.jsonl` | Per-tick campaign state. |
| `--log-every SEC` | `30` | Seconds between `[prog]` lines and progress-log records. |
| `--verbose` | off | Extra per-event prints to stderr. |

### 4.3 Campaign shape

| Flag | Default | What |
|------|---------|------|
| `--timeout SEC` | `3600` | Global wall-clock cap. |
| `--stall-cycles N` | `500` | Cycles of no new coverage before BMC is invoked. |
| `--stall-source {tuple,native,any}` | `tuple` | Which coverage signal the stall timer watches. |
| `--step-batch N` | `16` | Cycles per `step_trace` request (trades RPC chatter for responsiveness). |
| `--coverage-poll N` | `10` | Poll native coverage every N batches. |
| `--native-coverage` | off | Enable the simulator's native coverage channel. |
| `--no-coverage-target` | off | Disable coverage-bin-directed BMC targeting. |

### 4.4 BMC

| Flag | Default | What |
|------|---------|------|
| `--bmc-max-steps N` | `40` | Default depth cap (used only when tiers are off). |
| `--bmc-timeout MS` | `30000` | Default per-call timeout (used only when tiers are off). |
| `--bmc-depth-tiers STR` | `"32:5000,64:15000,128:60000"` | Tier table. Set to `"off"`/`"none"` to disable tiering. |
| `--bmc-unproductive-limit N` | `5` | Consecutive SAT-unproductive calls before campaign terminates. |

### 4.5 Corpus & mutation

| Flag | Default | What |
|------|---------|------|
| `--restore-mode {auto,replay,xsim,off}` | `auto` | Corpus restore strategy: waveform-first-then-replay, replay-only, xsim-checkpoint-only, or disable restore. |
| `--wdb-min-depth N` | `16` | Minimum path length before a waveform snapshot is considered worth storing. |
| `--wdb-budget-mb N` | `1024` | LRU cap for the waveform cache. |
| `--mutation-rate F` | `0.1` | Per-iteration probability of running a mutation burst. |
| `--mutation-burst N` | `20` | Cycles per mutation burst. |
| `--uniform-sampling` | off | Sample corpus entries uniformly instead of weighted by contribution. |

### 4.6 Seed stimuli (lever 3)

Structured extreme-value bursts injected before the main loop.

| Flag | Default | What |
|------|---------|------|
| `--seed-stimuli / --no-seed-stimuli` | on | Master toggle. |
| `--seed-cycles N` | `80` | Cycles each pinned-port seed holds its pin value. |
| `--seed-cross-product / --no-seed-cross-product` | off | *(Lever 6.)* Additionally emit two-port pair extremes (can be expensive on wide designs). |

### 4.7 BMC targeting levers (levers 4–8)

| Flag | Default | What |
|------|---------|------|
| `--bmc-target-rarity / --no-bmc-target-rarity` | off | *(Lever 4.)* Prefer uncovered bins whose sibling bits are already covered over bins in cold/dead registers. |
| `--bmc-corpus-precheck / --no-bmc-corpus-precheck` | off | *(Lever 5.)* Before invoking BMC, scan visited states for one that already satisfies the target; if found, force-restore it and skip the BMC call. |
| `--bmc-witness-mutations N` | `0` | *(Lever 7.)* After each productive BMC replay, replay N single-bit mutations of the witness to squeeze more coverage per solve. |
| `--bmc-per-reg-budget N` | `0` (unlimited) | *(Lever 8.)* Cap BMC attempts per arch-name register; prevents one dense register from starving the rest of the target pool. |

### 4.8 Value-class coverage (Phases 1–3 weak)

| Flag | Default | What |
|------|---------|------|
| `--value-class-coverage / --no-value-class-coverage` | off | *(Phase 1.)* Record per-bin signature diversity; add `vc=X/Y(Z.Z%)` to the progress line. |
| `--value-class-target N` | `4` | Signatures per bin required for diversification. Higher → more honest, slower to reach. |
| `--value-class-stall / --no-value-class-stall` | off | *(Phase 2.)* Stall on diversification growth instead of toggle growth. |
| `--value-class-bias / --no-value-class-bias` | off | *(Phase 3-weak.)* Periodically inject seed bursts whose inputs target missing signatures on under-diversified bins. |
| `--value-class-bias-every N` | `2000` | Cycles between diversification-bias passes. |

### 4.9 Differential testing

| Flag | Default | What |
|------|---------|------|
| `--predicate-module PATH` | (none) | *(Diff mode 1.)* Load a Python file defining `check(state, inputs) -> bool | None`; violations are logged to `violations.jsonl`. |
| `--differential-cross-sim / --no-differential-cross-sim` | off | *(Diff mode 3.)* Mirror every step to the alternate backend (Verilator↔xsim); log divergences to `differential.jsonl`. |

---

## 5. Output artefacts

When a campaign finishes (or is killed), `<output-dir>/` contains:

| Path | Purpose |
|------|---------|
| `progress.jsonl` | One record per progress tick. Cumulative campaign state. |
| `coverage.db` | Sqlite: visited states, coverage points, coverage universe, corpus entries, value-class hits. |
| `bmc.jsonl` | One record per BMC invocation (target, outcome, duration, depth, productivity). |
| `violations.jsonl` | Predicate violations (only if `--predicate-module`). |
| `differential.jsonl` | Cross-sim divergences (only if `--differential-cross-sim`). |
| `meta.json` | Resolved args, design metadata, timestamp. |
| `final_coverage.json` | Per-kind coverage snapshot at exit. |
| `verilator/` or `tcl_bridge_<top>.tcl` | The compiled harness or rendered TCL bridge. |
| `wdb/` | Waveform-database checkpoints (Verilator only, if corpus is on). |
| `uvm/` | UVM testbench scaffold (unless `--no-uvm`). |
| `cross_<backend>/` | Artefacts for the secondary differential driver (only if cross-sim on). |

### 5.1 Reading `progress.jsonl`

Each line is one JSON object. Key fields:

```json
{
  "t": 123.4,
  "cycle": 8700,
  "cps": 70.5,
  "states": 912,
  "native_covered": 2301,
  "native_total": 2519,
  "corpus_size": 891,
  "bmc_invocations": 18,
  "bmc_successes": 11,
  "bmc_avg_s": 4.8,
  "mutations": 3,
  "poll_ms": 0,
  "vc_diversified": 1847,
  "vc_total": 2519
}
```

The `vc_*` fields only appear with `--value-class-coverage`.

### 5.2 Reading `bmc.jsonl`

Every BMC call produces a record. Key fields:

```json
{
  "t": 123.4,
  "outcome": "sat",
  "kind": "coverage_toggle",
  "target": {"res_q": {"bit": 5, "val": 1, "flip": true}},
  "duration_ms": 4023.1,
  "depth": 7,
  "target_point_id": "xtgl:TOP.serdiv:res_q:5:rise",
  "target_hit": true,
  "direct_hit": true,
  "productive": true,
  "native_delta": 1,
  "states_delta": 8
}
```

UNSAT and timeout outcomes omit the replay-specific fields.

### 5.3 Reading violations / divergences

`violations.jsonl`:
```json
{
  "cycle": 473,
  "source": "step",
  "state": {"cnt_q": 65, "..." : "..."},
  "inputs": {"op_a_i": 0, "..." : "..."},
  "reason": "returned False"
}
```

`differential.jsonl`:
```json
{
  "cycle": 8100,
  "inputs": {"op_a_i": 42, "..." : "..."},
  "diffs": {"res_q": [12345, 54321]}
}
```

---

## 6. Writing a predicate module

Save anywhere convenient; point `--predicate-module path.py` at it:

```python
# my_checks.py

def check(state: dict, inputs: dict):
    """
    Return False (or raise) to log a violation.
    Return True (or None) for no violation.

    *state*  – {arch_name: int} snapshot after applying *inputs*.
    *inputs* – {port_name: int} just applied to the DUT.
    """
    # Example spec check: reset should keep counter at zero.
    if inputs.get("rst_n", 1) == 0 and state.get("cnt_q", 0) != 0:
        return False

    # Example protocol check: out_vld should imply in_rdy low.
    if state.get("out_vld_o", 0) and state.get("in_rdy_o", 0):
        return False

    return True
```

The callable is invoked every cycle. Throw or return False to log the violation; the campaign continues running so you can collect many violations in one run.

---

## 7. Running parallel ablations

The repository ships with `run_armH_parallel.sh`, a Bash runner that launches up to five independent campaigns pinned to disjoint CPU blocks via `taskset`. Each campaign points at a separate YAML (`serdiv_armH1.yaml` … `serdiv_armH5.yaml`) differing only in which lever is toggled on. To adapt for your design:

1. Copy one of the `serdiv_armH*.yaml` files to `<yourdesign>_armH*.yaml`.
2. Edit the `top`, `verilog`, and `output_dir` keys.
3. Keep the single differing lever per file; everything else should match the baseline.
4. Edit the `CORES` associative array in the script if your CPU count is not 32.
5. Run `./run_armH_parallel.sh` and watch with `tail -f <design>_armH{1..5}/run.log`.

On a 32-core host, five arms using `verilator_threads: 4` each fit in 20 cores, leaving 12 for the OS and the orchestrator's Python threads. Each arm owns its `<design>_armHN/` directory; no shared state.

---

## 8. Common problems and workarounds

### 8.1 Yosys rejects SystemVerilog

Symptoms:
```
ERROR: … syntax error, unexpected TOK_…
```

Pass `--sv2v` to preprocess with `sv2v` before Yosys. The converted file is cached under `sv2v_cache/<top>__sv2v.v` and reused across runs. If you don't have `sv2v`, install it from [github.com/zachjs/sv2v](https://github.com/zachjs/sv2v).

### 8.2 BMC is stalling the campaign

If you see `avg=30.0s` BMC calls flooding the log and no new coverage:

- Lower the top tier: `--bmc-depth-tiers "32:5000,64:15000"`.
- Tighten the unproductive limit: `--bmc-unproductive-limit 5`.
- Enable lever 5 to skip BMC when the corpus already has the target bit: `--bmc-corpus-precheck`.

### 8.3 Campaign terminates too early on "no_productive_bmc"

Either the unproductive limit is too low, or tier-promotion isn't resetting streaks properly. Check the log for `promoted to tier` lines. If you see many `SAT but unproductive → exhausted` at tier 0, consider enabling tiers (they promote instead of blacklisting at lower tiers).

### 8.4 Verilator `database is locked`

Rare. Happens when the coverage DB holds an uncommitted transaction while corpus writes. If it hits you, run with `--no-corpus` to isolate; in production, update to the latest SymbFuzz (the orchestrator commits after each value-class write).

### 8.5 xsim compilation slow

`xvlog` + `xelab` can take minutes on large designs. Use `--no-compile` to reuse an existing snapshot across runs of the same design.

---

## 9. Known limitations

- **Single-clock designs only.** All `step`/`step_trace` commands pulse exactly one clock once.
- **No multi-process corpus sharing.** Parallel ablations each maintain their own corpus.
- **BMC model divergence.** The BMC uses `clk2fflogic`, which treats non-synchronous constructs (latches, combinational loops, SV assertions) differently from most simulators. See [ARCHITECTURE.md §7](ARCHITECTURE.md) for the replay-alignment mitigation.
- **Native coverage on xsim requires HTML parse.** The xsim backend writes coverage to a database, calls `xcrg`, and parses the emitted HTML. If your Vivado install omits `xcrg`, native coverage is unavailable on xsim.
- **Value-class coverage is coarse on the first poll.** Many bins discover simultaneously; the current signature buffer is attributed to all of them. Raise `--value-class-target` (to 16 or higher) to get a meaningful post-initial-sweep metric.
- **Checkpoint/restore on xsim is unsupported.** Corpus restore on xsim replays the full input log from reset each time; slower than Verilator's waveform restore.
- **Mutation-testing differential mode** is not yet shipped.

---

## 10. Environment variables

| Variable | Effect |
|----------|--------|
| `SYMFUZZ_BMC` | Override path to the `symbfuzz` binary. Default: `<repo>/build/symbfuzz`. |
| `SV2V` | Override path to the `sv2v` binary. Default: first `sv2v` on `$PATH`. |
| `VERILATOR_ROOT` | Passed through to Verilator's own build; usually not needed. |

---

## 11. Getting help

- `symfuzz --help` — full up-to-date flag list.
- `docs/ARCHITECTURE.md` — design-level overview.
- `docs/TECHNICAL.md` — per-file/per-function reference.
- The source is small and well-commented; `symfuzz/orchestrator.py` is the single-largest file and reads top-to-bottom.
