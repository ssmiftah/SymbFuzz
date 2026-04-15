# SymbFuzz — Coverage-Directed Test Generation for Verilog/SystemVerilog

SymbFuzz is a research tool that automatically generates input stimulus for digital hardware designs to maximise state coverage. It combines two complementary techniques:

- **Random simulation** via Vivado xsim to explore easily-reachable states quickly.
- **Bounded Model Checking (BMC)** via Yosys + Z3 to escape coverage plateaus and reach hard-to-reach states.

The campaign runs until every architectural register combination has been visited, a global timeout elapses, or all remaining targets are proven unreachable.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Prerequisites](#2-prerequisites)
3. [Building an Executable](#3-building-an-executable)
4. [Quick Start](#4-quick-start)
5. [CLI Reference](#5-cli-reference)
6. [Multi-File / Multi-Module Designs](#6-multi-file--multi-module-designs)
7. [Interpreting Campaign Output](#7-interpreting-campaign-output)
8. [Coverage Database](#8-coverage-database)
9. [Python Package API](#9-python-package-api)
10. [C++ BMC Binary (`symbfuzz`)](#10-c-bmc-binary-symbfuzz)
11. [File-by-File Reference](#11-file-by-file-reference)
12. [Algorithm Details](#12-algorithm-details)
13. [Known Limitations](#13-known-limitations)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                       symfuzz campaign loop                         │
│                                                                     │
│  ┌──────────────┐   random inputs   ┌──────────────────────────┐    │
│  │  Orchestrator│ ────────────────► │  Vivado xsim (SimDriver) │    │
│  │              │ ◄──────────────── │  TCL bridge over JSON    │    │
│  │  (coverage   │   {state dict}    └──────────────────────────┘    │
│  │   stall?)    │                                                   │
│  │              │   target dict     ┌──────────────────────────┐    │
│  │              │ ────────────────► │  symbfuzz BMC binary     │    │
│  │              │ ◄──────────────── │  Yosys → SMT2 → Z3       │    │
│  │              │   input sequence  └──────────────────────────┘    │
│  │              │                                                   │
│  │              │   replay seq.     ┌──────────────────────────┐    │
│  │              │ ────────────────► │  StateForcer (xsim replay│    │
│  └──────┬───────┘                   └──────────────────────────┘    │
│         │                                                           │
│         ▼                                                           │
│  ┌──────────────┐                                                   │
│  │  CoverageDB  │  SQLite — visited states, coverage log            │
│  └──────────────┘                                                   │
└─────────────────────────────────────────────────────────────────────┘
```

**Key data flow:**

1. `parse_design()` runs Yosys on the Verilog source(s), produces an SMT2 model, and extracts port/register metadata into a `DesignInfo` object.
2. `testbench_gen.py` generates a Vivado xsim TCL bridge and compile script from Jinja2 templates.
3. `SimDriver` compiles the design with xvlog/xelab, then spawns the xsim process and communicates with it over line-delimited JSON via stdin/stdout.
4. `Orchestrator` runs the campaign loop: random steps → stall detection → BMC invocation → replay → repeat.
5. `CoverageDB` persists every observed register-value tuple to SQLite and guides target selection.

---

## 2. Prerequisites

### Required tools

| Tool | Version tested | Purpose |
|------|---------------|---------|
| Vivado / xsim | 2025.2 | RTL simulation backend (default) |
| Verilator | ≥ 5.0 | Alternative RTL simulation backend (recommended for speed) |
| Yosys | ≥ 0.35 | RTL → SMT2 compilation |
| Z3 | ≥ 4.12 | SMT solving (C++ library + `z3++.h` header) |
| CMake | ≥ 3.16 | C++ build system |
| GCC or Clang | C++20 capable | C++ compiler |
| Python | 3.10+ | Campaign orchestration driver |
| setuptools | ≥ 68 | Python package build backend |
| Jinja2 | any recent | Testbench template rendering |

### Install system dependencies (Ubuntu / Debian)

```bash
# Yosys
sudo apt install yosys

# Z3 solver + development headers
sudo apt install libz3-dev z3

# CMake and C++ compiler (if not already present)
sudo apt install cmake build-essential
```

### Vivado on PATH

Vivado must be sourced before running any SymbFuzz campaign:

```bash
source /path/to/Vivado/2025.2/settings64.sh
```

Add this line to your `~/.bashrc` / `~/.zshrc` to make it permanent.

---

## 3. Building an Executable

SymbFuzz has two components that must both be built: the **C++ BMC binary** and the **Python CLI executable**. Build them in order.

### Step 1 — C++ BMC binary (`symbfuzz`)

The BMC engine is a standalone C++ binary that links against Yosys and Z3.

```bash
cd /path/to/SymbFuzz

# Create and enter the build directory
mkdir -p build && cd build

# Configure (Release mode by default)
cmake .. -DCMAKE_BUILD_TYPE=Release

# Compile using all available CPU cores
make -j$(nproc)

# Return to project root
cd ..
```

This places the binary at `build/symbfuzz`. The Python CLI looks for it there automatically — no further steps are needed for normal use.

**Build types:**

| Type | Compiler flags | Use when |
|------|---------------|---------|
| `Release` | `-O3 -DNDEBUG` | Normal / production use |
| `Debug` | `-g -O0 -fsanitize=address,undefined` | Debugging crashes or assertion failures |

To build in Debug mode:

```bash
cmake .. -DCMAKE_BUILD_TYPE=Debug && make -j$(nproc)
```

**System-wide install (optional):**

```bash
# Installs symbfuzz to /usr/local/bin (requires sudo)
sudo cmake --install build

# Or install to a user-local prefix (no sudo needed)
cmake --install build --prefix ~/.local
```

---

### Step 2 — Python CLI executable (`symfuzz` command)

The project ships a `pyproject.toml` that registers `symfuzz` as a console script entry point. Install the package in editable mode into a virtual environment:

```bash
cd /path/to/SymbFuzz

# Create virtual environment (first time only)
python3 -m venv .venv

# Activate the environment
source .venv/bin/activate   # Linux / macOS
# .venv\Scripts\activate    # Windows

# Install SymbFuzz and its Python dependencies
pip install -e .
```

`pip install -e .` installs the package in **editable mode** (source changes are reflected immediately without reinstalling) and creates a `symfuzz` executable at `.venv/bin/symfuzz`.

After activating the venv, run campaigns with:

```bash
symfuzz examples/counter.v --top counter --verbose
```

**What `pyproject.toml` declares:**

| Field | Value |
|-------|-------|
| Package name | `symfuzz` |
| Version | `0.1.0` |
| Python requirement | `>= 3.10` |
| Runtime dependency | `jinja2` |
| Console entry point | `symfuzz` → `symfuzz.cli:main` |
| Included data files | `symfuzz/templates/*.j2` |

---

### Step 3 — Verify the build

```bash
# Verify C++ binary
./build/symbfuzz --help

# Verify Python command (venv must be active)
symfuzz --help

# Quick end-to-end smoke test
symfuzz examples/counter.v --top counter --timeout 30 --stall-cycles 100
```

Expected final line: `Coverage : 100.0%`

---

### Optional — Standalone portable binary (PyInstaller)

To produce a single self-contained executable that does not require a Python venv on the target machine:

```bash
pip install pyinstaller

pyinstaller --onefile --name symfuzz \
    --add-data "symfuzz/templates:symfuzz/templates" \
    symfuzz_run.py
```

Output: `dist/symfuzz` — bundles Python + all dependencies. Distribute it alongside the separately compiled `build/symbfuzz` BMC binary.

---

## 4. Quick Start

### Single-file design

```bash
source .venv/bin/activate

symfuzz examples/counter.v \
    --top counter \
    --stall-cycles 200 \
    --timeout 60 \
    --verbose
```

Expected output (abbreviated):

```
[symfuzz] Parsing design: ['/.../counter.v'] (top: counter)
[symfuzz] Module    : counter
[symfuzz] Inputs    : ['clk', 'en', 'rst']
[symfuzz] Outputs   : ['count']
[symfuzz] Registers : ['count']
[symfuzz] Clock     : clk
[symfuzz] Reset     : rst
[symfuzz] State space: 16 states
[symfuzz] Generating Vivado xsim harness → counter_symfuzz/
[symfuzz] Compiling design (xvlog + xelab) ...
[symfuzz] Starting campaign (stall=200 cycles, timeout=60.0s)
[orch] cycle     1  NEW state: {'count': 1} ...
...
============================================================
 CAMPAIGN RESULTS
============================================================
  Terminated by  : full_coverage
  Total cycles   : 31
  Elapsed time   : 3.9s
  States visited : 16 / 16
  Coverage       : 100.0%
  BMC invocations: 0 (0 successful)
  Coverage DB    : counter_symfuzz/coverage.db
============================================================
```

### Multi-file design

```bash
symfuzz \
    RTL_Tst_Cases/multi_mod/sat_counter.v \
    RTL_Tst_Cases/multi_mod/dual_counter.v \
    --top dual_counter \
    --stall-cycles 60 \
    --verbose
```

Pass **sub-module files before the top-level file**. All files are compiled with xvlog in order.

### Config file (YAML)

Any CLI flag may be supplied through a YAML config file via `--config`, with CLI arguments taking precedence. Positional Verilog sources use the `verilog` key (a list). Example:

```yaml
# campaign.yaml
top: serdiv
sv2v: true
verilog:
  - RTL_Tst_Cases/cva6-5.3.0/core/include/config_pkg.sv
  - RTL_Tst_Cases/cva6-5.3.0/core/include/riscv_pkg.sv
  - RTL_Tst_Cases/cva6-5.3.0/core/serdiv.sv
timeout: 300
stall_cycles: 500
restore_mode: replay
mutation_rate: 0.15
mutation_burst: 25
step_batch: 16
```

```bash
symfuzz --config campaign.yaml                   # use the file's values
symfuzz --config campaign.yaml --timeout 60      # CLI overrides the file
```

Precedence: argparse defaults < YAML values < explicit CLI flags. Unknown YAML keys are reported as errors.

### SystemVerilog design (with sv2v)

Designs using SystemVerilog constructs Yosys cannot parse (`parameter type`, struct casts, complex package functions) must be preprocessed with [`sv2v`](https://github.com/zachjs/sv2v) first. Pass `--sv2v` and list package files before the top-level module:

```bash
symfuzz \
    RTL_Tst_Cases/cva6-5.3.0/core/include/config_pkg.sv \
    RTL_Tst_Cases/cva6-5.3.0/core/include/riscv_pkg.sv \
    RTL_Tst_Cases/cva6-5.3.0/core/include/cv64a6_imafdc_sv39_config_pkg.sv \
    RTL_Tst_Cases/cva6-5.3.0/core/include/ariane_pkg.sv \
    RTL_Tst_Cases/cva6-5.3.0/vendor/pulp-platform/common_cells/src/cf_math_pkg.sv \
    RTL_Tst_Cases/cva6-5.3.0/vendor/pulp-platform/common_cells/src/lzc.sv \
    RTL_Tst_Cases/cva6-5.3.0/core/serdiv.sv \
    --top serdiv --sv2v --timeout 300
```

sv2v is installed automatically when the Python venv is active:

```bash
pip install sv2v-python   # or: apt install sv2v / download binary from GitHub
```

The converted Verilog is cached to `sv2v_cache/<top>__sv2v.v` so subsequent runs skip reconversion.

---

## 5. CLI Reference

```
symfuzz [OPTIONS] <file1.v> [<file2.v> ...]
```

### Positional arguments

| Argument | Description |
|----------|-------------|
| `<file1.v> ...` | One or more Verilog/SystemVerilog source files. Pass dependencies before the top-level file. All files are read by Yosys and compiled by xvlog. |

### Required options

| Option | Description |
|--------|-------------|
| `--top <module>` | Top-level module name |

### Optional options

| Option | Default | Description |
|--------|---------|-------------|
| `--output-dir <dir>` | `<module>_symfuzz/` | Directory for generated testbench artifacts and coverage database |
| `--stall-cycles <N>` | `500` | Number of cycles without new coverage before invoking BMC |
| `--bmc-max-steps <N>` | `40` | Maximum BMC depth per invocation |
| `--bmc-timeout <ms>` | `30000` | Per-BMC-call Z3 timeout in milliseconds |
| `--timeout <sec>` | `3600` | Global campaign wall-clock timeout in seconds |
| `--no-compile` | off | Skip xvlog/xelab compilation (reuse existing snapshot) |
| `--no-uvm` | off | Skip UVM testbench generation |
| `--gen-only` | off | Generate TB artifacts and exit without running simulation |
| `--sv2v` | off | Preprocess sources with `sv2v` before Yosys. Required for SystemVerilog constructs Yosys cannot parse (`parameter type`, struct casts, complex package functions). `sv2v` must be on PATH. |
| `--sim` | `xsim` | Simulation backend. `xsim` uses Vivado xsim + TCL bridge. `verilator` compiles a Verilator C++ harness and drives it via the same JSON protocol — typically far faster per cycle on synchronous RTL. |
| `--verilator-threads` | `1` | Number of worker threads for Verilator's multi-threaded simulation model (`--threads` at compile time). `1` = single-threaded. Benefits designs with abundant parallel combinational logic; small designs (< ~1000 flops) typically see no benefit or a slight regression from scheduling overhead. Ignored for `--sim xsim`. |
| `--native-coverage` | off | Enable simulator-native line + toggle coverage. On xsim compiles with `--cc_type st` and polls via `write_xsim_coverage` + `xcrg` post-processing. On Verilator compiles with `--coverage-line --coverage-toggle` and polls via `VerilatedCov::write`. Adds noticeable per-poll overhead on xsim (xcrg spawn); negligible on Verilator. |
| `--uniform-sampling` | off | Force uniform corpus sampling, bypassing the rarity-weighted scheduler. Used as an A/B control switch; default (off) samples entries proportional to their contribution score / (1 + hit_count). |
| `--no-coverage-target` | off | Disable coverage-driven BMC targeting. BMC falls back to the legacy register-value target picker. A/B control arm; requires `--native-coverage` to be meaningful. |
| `--bmc-unproductive-limit` | `5` | Terminate the campaign after N consecutive BMC calls that returned SAT but whose replay added no new coverage. The unproductive target is also retired immediately on each such call. `0` disables (legacy behavior: retry indefinitely). |
| `--stall-source` | `tuple` | What resets the stall counter. `tuple` (default) uses new register-tuple discoveries (legacy). `native` uses new native-coverage points (requires `--native-coverage`); best for large designs where tuple discoveries never plateau. `any` resets on either. |
| `--log-every` | `30` | Seconds between `[prog]` progress lines emitted to stderr. `0` disables. |
| `--progress-log` | `<out>/progress.jsonl` | Append a JSON-per-line progress record at each tick. Empty string disables the file (stderr line stays). |
| `--coverage-poll` | `10` | Poll native coverage every N batches when `--native-coverage` is on. |
| `--grey-bit-samples` | `4` | For xcrg "Grey" (partially-covered) "All bits of `<sig>`" rows, emit only this many stratified per-bit coverage points instead of all `W`. Reduces false-uncovered noise that prematurely trips the unproductive streak. `0` = full per-bit expansion. |
| `--restore-mode` | `auto` | Corpus/checkpoint tier. `auto` / `xsim` currently downgrade to `replay` because xsim 2025.2 lacks simulator-state checkpoint APIs. `replay` uses durable RLE input-sequence replay. `off` disables corpus + resume. |
| `--wdb-min-depth` | `20` | Reserved for Tier-2 `.wdb` caching when available. Unused under `replay`. |
| `--wdb-budget-mb` | `500` | LRU disk budget for Tier-2 `.wdb` cache when available. Unused under `replay`. |
| `--mutation-rate` | `0.1` | Probability per cycle of running an AFL-style mutation burst from a sampled corpus entry. `0.0` reproduces legacy pure-random behavior. |
| `--mutation-burst` | `20` | Maximum cycles per mutation burst. |
| `--step-batch` | `16` | Cycles per batched `step_trace` call — collapses N stdio roundtrips into 1. Set to `1` for legacy single-step behavior. Clamped to `stall_cycles`. |
| `--clock-period` | `"1 ps"` | TCL `run` time unit inside `clk_posedge`. Increase (e.g. `"5 ns"`) if your RTL has `#delays` that depend on clock period. |
| `--config <file.yaml>` | — | Supply any flag via YAML. CLI args override file values. Keys use underscores in place of dashes. |
| `--verbose`, `-v` | off | Print per-cycle coverage events and BMC invocations |

### Examples

```bash
# Generate artifacts only, do not simulate
symfuzz examples/lock_fsm.v --top lock_fsm --gen-only

# Reuse existing xsim snapshot (skip recompile)
symfuzz examples/counter.v --top counter --no-compile

# Aggressive BMC: longer depth, more stall patience
symfuzz examples/lock_fsm.v --top lock_fsm \
    --stall-cycles 1000 --bmc-max-steps 80 --bmc-timeout 60000

# Custom output directory
symfuzz src/cpu.v pkg/types.sv --top cpu --output-dir /tmp/cpu_fuzz

# SystemVerilog design requiring sv2v preprocessing
symfuzz pkg/config_pkg.sv rtl/my_module.sv --top my_module --sv2v
```

---

## 6. Multi-File / Multi-Module Designs

SymbFuzz fully supports designs spread across multiple Verilog/SystemVerilog files.

### Ordering rule

Pass files from **least dependent to most dependent** — sub-modules before the top. This is the same order `xvlog` expects.

```bash
symfuzz \
    rtl/pkg_types.sv \      # package / typedefs
    rtl/alu.v \             # sub-module
    rtl/reg_file.v \        # sub-module
    rtl/cpu.v \             # top-level (depends on all above)
    --top cpu
```

### What happens internally

1. **Yosys** reads all files in order, checks hierarchy, flattens into one module, converts to SMT2.
2. **xvlog** analyses each file separately (same order).
3. **xelab** elaborates the flattened top-level snapshot.
4. The **BMC binary** receives all source files and re-runs the same Yosys pass internally to build its own SMT2 model.

### Hierarchical register names

After flattening, Yosys names sub-instance registers with dot notation (e.g., `u_a.count` for instance `u_a`'s signal `count`). SymbFuzz:

- Uses `u_a.count` as the **arch_name** (coverage key, JSON field name, BMC target name).
- Converts dots to slashes (`u_a/count`) for **xsim signal paths** (`get_value -radix unsigned /dual_counter/u_a/count`).

This conversion is handled automatically by `RegisterInfo.xsim_path`.

---

## 7. Interpreting Campaign Output

### Verbose per-cycle log

```
[orch] cycle     42  NEW state: {'count': 5}  total=6  (37.5%)
```

| Field | Meaning |
|-------|---------|
| `cycle` | Simulation cycle number (resets to 0 after each BMC replay) |
| `NEW state` | Register values of the newly-discovered state |
| `total` | Total unique states visited so far |
| `(37.5%)` | Coverage percentage of the finite state space |

### BMC log lines

```
[orch] Stalled for 200 cycles — invoking BMC ...
[bmc] /path/to/symbfuzz file.v --top m --target count=12 ...
[orch] BMC: found path of depth 15 to {'count': 12}
[orch] Replaying BMC sequence (depth=15) ...
```

```
[orch] BMC: no path to {'count': 15} — marked exhausted
```

If a target is retried three times without the replay reaching it, it is also marked exhausted:

```
[orch] Target {'u_a.count': 6, 'u_b.count': 5} failed 3 replays — marked exhausted
```

### Final banner

```
============================================================
 CAMPAIGN RESULTS
============================================================
  Terminated by  : full_coverage        ← or "timeout" / "no_more_targets"
  Total cycles   : 485
  Elapsed time   : 22.3s
  States visited : 64 / 64
  Coverage       : 100.0%
  BMC invocations: 8 (8 successful)
  Coverage DB    : dual_counter_symfuzz/coverage.db
============================================================
```

**Termination reasons:**

| Reason | Meaning |
|--------|---------|
| `full_coverage` | Every state in the finite state space was visited |
| `timeout` | Wall-clock timeout (`--timeout`) reached |
| `no_more_targets` | All reachable states are covered; remaining targets proven unreachable by BMC |

---

## 8. Coverage Database

The SQLite database at `<output-dir>/coverage.db` persists the campaign state.

### Tables

**`visited_states`**

| Column | Type | Description |
|--------|------|-------------|
| `state_hash` | INTEGER PK | Signed 64-bit SHA-256 prefix of the state JSON |
| `state_json` | TEXT | JSON encoding of `{reg_name: value, ...}` |
| `first_seen` | REAL | `time.monotonic()` timestamp |
| `cycle_count` | INTEGER | Simulation cycle when first discovered |

**`coverage_log`**

| Column | Type | Description |
|--------|------|-------------|
| `cycle` | INTEGER | Simulation cycle |
| `new_states` | INTEGER | States added this entry (always 1) |
| `total_states` | INTEGER | Running total |

### Querying manually

```bash
sqlite3 counter_symfuzz/coverage.db

# All visited states
SELECT state_json, cycle_count FROM visited_states ORDER BY cycle_count;

# Coverage over time
SELECT cycle, total_states FROM coverage_log ORDER BY cycle;
```

### Resuming a campaign

Delete (or move) `coverage.db` before rerunning if you want a fresh start. With `--no-compile` and an existing database, the campaign resumes from the previously-reached coverage level.

---

## 9. Python Package API

All classes live under `symfuzz/`. Import pattern:

```python
from symfuzz.design_parser import parse_design, DesignInfo
from symfuzz.sim_driver    import SimDriver
from symfuzz.bmc_interface import BmcInterface, BmcResult
from symfuzz.coverage_db   import CoverageDB
from symfuzz.orchestrator  import Orchestrator, CampaignResult
from symfuzz.state_forcing import StateForcer
from symfuzz.testbench_gen import generate_vivado_harness, generate_uvm_testbench
```

### `design_parser.parse_design()`

```python
design = parse_design(
    verilog_files,      # str | Path | list[str | Path]
    top_module=None,    # str — top module name (auto-detect if omitted)
    flatten=True,       # bool — flatten hierarchy before SMT2 export
    use_sv2v=False,     # bool — preprocess with sv2v before Yosys
)
```

Returns a `DesignInfo` dataclass:

| Field | Type | Description |
|-------|------|-------------|
| `module_name` | `str` | Top module name |
| `verilog_path` | `str` | Primary source file |
| `verilog_files` | `list[str]` | All source files in dependency order |
| `inputs` | `list[PortInfo]` | All input ports |
| `outputs` | `list[PortInfo]` | All output ports |
| `registers` | `list[RegisterInfo]` | Architectural registers identified by Yosys |
| `clock_port` | `str` | Detected clock port name |
| `reset_port` | `str \| None` | Detected reset port name |
| `smt2_text` | `str` | Raw SMT2 output from Yosys |
| `data_inputs` | property | `inputs` minus the clock port |
| `max_state_space` | property | Product of `2^width` for each register |

#### `PortInfo`

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Port name |
| `width` | `int` | Bit width |
| `direction` | `str` | `'input'` or `'output'` |
| `mask` | property | `(1 << width) - 1` |

#### `RegisterInfo`

| Field | Type | Description |
|-------|------|-------------|
| `arch_name` | `str` | RTL signal name, e.g. `count`, `u_a.count` |
| `mangled_name` | `str` | Full Yosys SMT2 mangled name |
| `width` | `int` | Bit width |
| `returns_bool` | `bool` | True if the SMT2 accessor returns `Bool` (1-bit) |
| `xsim_path` | property | Signal path for xsim (`u_a.count` → `u_a/count`) |
| `mask` | property | `(1 << width) - 1` |

---

### `sim_driver.SimDriver`

Manages the Vivado xsim subprocess. Communicates via line-delimited compact JSON.

```python
driver = SimDriver(design, tb_dir, verbose=False)
driver.compile()          # run compile_<module>.sh (xvlog + xelab)
driver.load()             # spawn xsim process
driver.reset(cycles=5)    # assert reset for N clock cycles
driver.read_state()       # → dict[str, int]: current register values
driver.step(inputs)       # → dict[str, int]: one clock edge with given inputs
driver.random_step()      # → dict[str, int]: one clock edge with random inputs
driver.force(state)       # write register values directly (state forcing)
driver.close()            # send quit, wait for process exit
```

**Protocol** (JSON over stdin/stdout):

```
→ {"cmd":"step","inputs":{"en":1,"rst":0}}          ← {"state":{"count":3}}
→ {"cmd":"step_trace","inputs":[{...},{...}]}       ← {"states":[{...},{...}]}
                                                      (+ "error" on partial)
→ {"cmd":"step_raw","inputs":{"clk":1,"en":1,...}}  ← {"state":{"count":3}}
→ {"cmd":"step_raw_trace","inputs":[{...},{...}]}   ← {"states":[{...},{...}]}
→ {"cmd":"reset","cycles":5}                         ← {"ok":true}
→ {"cmd":"read_state"}                               ← {"state":{"count":3}}
→ {"cmd":"force","state":{"count":7}}                ← {"ok":true}
→ {"cmd":"quit"}
```

- `step` / `step_trace` — run one full clock cycle per entry (`clk_posedge` wraps both edges). Used by random stepping, mutation, and corpus restore.
- `step_raw` / `step_raw_trace` — run **one SMT2 transition** per entry. The caller specifies `clk` explicitly in the input dict; the handler sets all inputs then does one `eval` / `run <period>`. Retained as a diagnostic primitive for SMT-semantic replay investigations; not used by the default BMC-replay path.

**BMC-replay alignment (Phase 5).** BMC's `clk2fflogic` model says "inputs at step k drive the transition to step k+1." Event-driven simulators (Verilator `always_ff @(posedge clk)`, xsim analogues) say "inputs *at* the posedge drive the transition." Replaying an SMT2 trace through the simulator with posedge-step inputs therefore latches the **wrong** cycle's inputs at each clock edge, and the simulator state diverges from BMC's SAT witness.

The orchestrator corrects the off-by-one by **cycle-wrapping**: `_bmc_steps_to_cycle_inputs` slides a 2-wide window over BMC's full step list and, for each `(clk=0, clk=1)` pair, emits the **negedge step's** non-clk inputs. The resulting one-dict-per-cycle list feeds through the regular `step_trace` primitive (which wraps clk 0→1 internally), so the simulator latches the inputs BMC actually intended.

Empirically on lock_fsm under Verilator:

| | pre-fix (posedge-step inputs or raw) | **post-fix (negedge-step inputs)** |
|---|---|---|
| `direct_hit` | 0/10 | **1/1 SAT agrees across `target_hit`, `direct_hit`, `productive`** |
| native coverage in 30 s | 37/58 (63.8 %) | **54/58 (93.1 %)** |

---

### `bmc_interface.BmcInterface`

Calls the `symbfuzz` binary as a subprocess.

```python
bmc = BmcInterface(
    symbfuzz_binary,    # path to the compiled binary
    design,             # DesignInfo
    max_steps=40,       # BMC depth bound
    timeout_ms=30000,   # Z3 timeout per call
    verbose=False,
)

result = bmc.find_sequence({"count": 12})
# Returns BmcResult or None if no path found
```

#### `BmcResult`

| Field | Type | Description |
|-------|------|-------------|
| `depth` | `int` | Number of SMT2 transitions in the found path |
| `steps` | `list[dict[str, int]]` | Per-cycle input values (clock filtered out, posedge-only) |
| `target` | `dict[str, int]` | The requested target constraints |

---

### `coverage_db.CoverageDB`

```python
db = CoverageDB(db_path, design)
db.record_state(state, cycle)           # → bool: True if new state
db.is_visited(state)                    # → bool
db.total_states()                       # → int
db.stale_since()                        # → int: cycles since last new state
db.coverage_pct()                       # → float: percentage of max_state_space
db.summary()                            # → str: human-readable summary line
db.mark_target_exhausted(target)        # mark a target as unreachable
db.get_unvisited_neighbor_target()      # → dict | None: next BMC target
db.close()
```

**Target selection strategy** (`get_unvisited_neighbor_target`):

1. Pick the register with the most unseen values.
2. Return the smallest unseen, non-exhausted value for that register as a single-register constraint (`{reg: val}`).
3. If all individual values have been observed (but joint states remain), enumerate joint combinations and return the first unvisited, non-exhausted combination.
4. Return `None` when all reachable states are covered.

---

### `orchestrator.Orchestrator`

```python
orch = Orchestrator(
    design, driver, coverage_db, bmc, forcer,
    stall_cycles=500,
    global_timeout=3600.0,
    verbose=False,
)
result = orch.run()   # → CampaignResult
```

#### `CampaignResult`

| Field | Type | Description |
|-------|------|-------------|
| `total_cycles` | `int` | Simulation cycles at termination |
| `total_states` | `int` | Unique states discovered |
| `coverage_pct` | `float` | Coverage percentage |
| `elapsed_sec` | `float` | Wall-clock time |
| `bmc_invocations` | `int` | Number of BMC calls made |
| `bmc_successes` | `int` | Number that returned a satisfying path |
| `terminated_by` | `str` | `"full_coverage"` \| `"timeout"` \| `"no_more_targets"` |

---

### `corpus_store.CorpusStore`

Unified checkpoint + AFL-style input corpus. One SQLite table (`corpus_entries`
in the coverage DB) keyed by `state_hash` holds an RLE-compressed input
sequence that reaches each frontier state. Used by the orchestrator to (a)
resume campaigns across process restarts, and (b) seed AFL-style mutation
bursts.

```python
corpus = CorpusStore(
    db_path       = coverage_db_path,
    wdb_dir       = output_dir / "wdb",
    design        = design,
    snapshot_file = snapshot_path,  # used for design-hash (Tier 2 only)
    mode          = "replay",       # or "off" / "auto" / "xsim"
    wdb_min_depth = 20,
    wdb_budget_mb = 500,
)

corpus.add_entry(state_hash, segments, source="bmc", driver=driver)
corpus.restore(state_hash, driver)        # True on hit
entry = corpus.sample(rng)                # uniform sample; bumps hit_count
entry = corpus.pick_deepest()             # deepest entry, or None
```

Input sequences are stored as RLE segments: `list[(inputs_dict, repeat_count)]`.
`append_input(segments, inputs)` is the in-place RLE extender used by the
orchestrator to build `current_path` cycle-by-cycle. Replay applies
`driver.reset()` then iterates the segments, stepping `repeat` times per
segment. Post-restore state-hash verification auto-evicts stale rows after
semantic RTL changes without wiping the store.

### `mutations`

```python
from symfuzz.mutations import havoc, splice

# Each strategy yields input dicts; the caller steps the driver.
for inputs in havoc(design, burst=20, rng=rng): ...
for inputs in splice(design, other_entry, burst=20, rng=rng): ...
```

`havoc` rolls fresh random inputs for up to `burst` cycles from the seed
state. `splice` grafts the tail of another corpus entry's input sequence
starting at a random offset. Both terminate early when the orchestrator
observes new coverage.

### `state_forcing.StateForcer`

```python
forcer = StateForcer(driver, design)

# Replay a BMC input sequence; returns ALL intermediate states (not just final)
states = forcer.replay_sequence(bmc_result, reset_first=True)
# → list[dict[str, int]]

# Directly write register values into simulation
state = forcer.force_state({"count": 7})
# → dict[str, int] (state after one settling cycle)
```

> **Note on replay semantics:** `replay_sequence` records every intermediate clock cycle, not only the final one. This maximises coverage progress even when the SMT2 model's predicted final state diverges from the RTL simulation due to `clk2fflogic` semantics.

---

### `testbench_gen`

```python
from symfuzz.testbench_gen import generate_vivado_harness, generate_uvm_testbench

tb_dir  = generate_vivado_harness(design, output_dir)
uvm_dir = generate_uvm_testbench(design, output_dir)
```

`generate_vivado_harness` produces:

| File | Description |
|------|-------------|
| `tcl_bridge_<module>.tcl` | xsim stdin/stdout JSON bridge |
| `compile_<module>.sh` | xvlog + xelab compile script |

`generate_uvm_testbench` produces a full UVM testbench skeleton under `<output_dir>/uvm/`:

```
<module>_if.sv        interface
<module>_seq_item.sv  sequence item
<module>_driver.sv    UVM driver
<module>_monitor.sv   UVM monitor
<module>_agent.sv     UVM agent
<module>_env.sv       UVM environment
<module>_rand_seq.sv  random sequence
<module>_bmc_seq.sv   BMC-guided sequence
<module>_test.sv      UVM test
tb_<module>_uvm.sv    testbench top
```

---

## 10. C++ BMC Binary (`symbfuzz`)

The `symbfuzz` binary is invoked by `BmcInterface`. It can also be called directly for one-off queries.

### Usage

```
symbfuzz [OPTIONS] <file1.v> [<file2.v> ...]
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--top <module>` | auto | Top-level module name |
| `--target <wire>=<val>` | none | Target constraint: full-width equality (repeatable; ANDed together). |
| `--target-bit <wire>=<bit>=<val>` | none | Bit-level constraint: assert bit `<bit>` of `<wire>` equals `<val>` (0 or 1). Repeatable; ANDed with other targets. Emits `(= ((_ extract K K) accessor) (_ bvV 1))` — pins one bit, leaves the rest free. Preferred by the Phase 1b coverage-driven target path. |
| `--target-flip <wire>=<bit>=<val>` | none | Temporal-flip constraint: at the final step bit `<bit>` of `<wire>` equals `<val>`, AND at the preceding step it equals the opposite. Forces a real *transition* between steps k-1 and k. Min depth clamped to 2. UNSATs cleanly on bits stuck at one value (true constants). |
| `--max-steps <N>` | `20` | BMC depth bound |
| `--timeout <ms>` | `30000` | Z3 timeout per call in milliseconds |
| `--output json` | `table` | Output format: `table` (human-readable) or `json` |
| `--no-flatten` | — | Disable hierarchy flattening |
| `--no-zero-init` | — | Allow non-zero initial register values |
| `--verbose` | — | Print Yosys log and BMC progress |
| `--help` | — | Show usage |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Satisfying input sequence found |
| `1` | Error (bad arguments, Yosys failure, etc.) |
| `2` | UNSAT — no path found within `--max-steps` |

### JSON output format (`--output json`)

```json
{
  "depth": 15,
  "steps": [
    {"clk": 1, "en": 1, "rst": 0},
    {"clk": 1, "en": 1, "rst": 0},
    ...
  ]
}
```

Each entry in `steps` is one SMT2 transition. Because `clk2fflogic` produces two transitions per clock cycle (negedge then posedge), `BmcInterface` filters to posedge-only steps automatically.

### Direct use examples

```bash
# Find how to reach count=10 in ≤15 cycles
./build/symbfuzz examples/counter.v --top counter \
    --target count=10 --max-steps 15

# Multi-file: find how to reach cnt_a=5 AND cnt_b=5
./build/symbfuzz \
    RTL_Tst_Cases/multi_mod/sat_counter.v \
    RTL_Tst_Cases/multi_mod/dual_counter.v \
    --top dual_counter \
    --target u_a.count=5 --target u_b.count=5 \
    --max-steps 30 --output json

# Debug: print Yosys log
./build/symbfuzz examples/counter.v --top counter \
    --target count=7 --verbose
```

---

## 11. File-by-File Reference

### Project root

| File | Role |
|------|------|
| `pyproject.toml` | Python package metadata, dependencies, and `symfuzz` console entry point |
| `CMakeLists.txt` | C++ build configuration for the `symbfuzz` BMC binary |
| `symfuzz_run.py` | Thin shim — `python symfuzz_run.py` is equivalent to the `symfuzz` command |
| `DOCUMENTATION.md` | This document |

### Python package (`symfuzz/`)

| File | Role |
|------|------|
| `cli.py` | Entry point. Parses arguments, orchestrates the full pipeline. |
| `design_parser.py` | Runs Yosys (optionally via sv2v preprocessing), parses SMT2 annotations, produces `DesignInfo`. |
| `testbench_gen.py` | Renders Jinja2 templates to produce xsim and UVM artifacts. |
| `sim_driver.py` | Compatibility shim that re-exports `drivers.xsim.XsimDriver` as `SimDriver`. |
| `drivers/__init__.py` | `make_driver(backend, ...)` factory — returns the right driver for `--sim xsim` or `--sim verilator`. |
| `drivers/base.py` | `BaseStdioDriver` — shared JSON protocol, step/step_trace/reset/read_state/force/checkpoint/restore. Backend-specific subclasses override only `compile()` and `load()`. |
| `drivers/xsim.py` | `XsimDriver` — spawns `xsim` + TCL bridge. |
| `drivers/verilator.py` | `VerilatorDriver` — spawns the Verilator-compiled C++ harness binary. |
| `bmc_interface.py` | Calls the `symbfuzz` binary, filters posedge steps from JSON output. |
| `coverage_db.py` | SQLite-backed coverage store with target selection logic. |
| `orchestrator.py` | Main campaign loop: random → stall → BMC → replay → repeat. |
| `state_forcing.py` | Replays a BMC input sequence into the live simulation. |
| `corpus_store.py` | Durable input-sequence corpus (`corpus_entries` table) doubling as checkpoint store and AFL seed pool. RLE compression, post-restore state verification, LRU eviction. |
| `mutations.py` | AFL-style mutation strategies (`havoc`, `splice`) that produce input dicts to step from a restored seed state. |

### Templates (`symfuzz/templates/`)

| File | Generated Output |
|------|-----------------|
| `tcl_bridge.tcl.j2` | `tcl_bridge_<module>.tcl` — xsim JSON bridge |
| `compile.sh.j2` | `compile_<module>.sh` — xvlog + xelab script |
| `verilator_harness.cpp.j2` | `verilator/harness.cpp` — Verilator C++ harness implementing the same JSON protocol |
| `verilator_compile.sh.j2` | `verilator/compile_<module>.sh` — `verilator --cc --build --exe` script |
| `uvm_if.sv.j2` | `<module>_if.sv` — SystemVerilog interface |
| `uvm_seq_item.sv.j2` | `<module>_seq_item.sv` — UVM sequence item |
| `uvm_driver.sv.j2` | `<module>_driver.sv` |
| `uvm_monitor.sv.j2` | `<module>_monitor.sv` |
| `uvm_agent.sv.j2` | `<module>_agent.sv` |
| `uvm_env.sv.j2` | `<module>_env.sv` |
| `uvm_rand_seq.sv.j2` | `<module>_rand_seq.sv` |
| `uvm_bmc_seq.sv.j2` | `<module>_bmc_seq.sv` |
| `uvm_test.sv.j2` | `<module>_test.sv` |
| `uvm_tb_top.sv.j2` | `tb_<module>_uvm.sv` |

### C++ sources (`src/` and `include/`)

| File | Role |
|------|------|
| `main.cpp` | CLI argument parsing, top-level pipeline orchestration |
| `frontend/yosys_driver.{h,cpp}` | Runs Yosys via shell, returns SMT2 text |
| `frontend/smt2_parser.{h,cpp}` | Parses SMT2 annotation comments into `DesignModel` |
| `bmc/smt2_builder.{h,cpp}` | Builds the BMC SMT2 query (unrolled transitions + goal) |
| `bmc/bmc_engine.{h,cpp}` | Iterative deepening BMC loop |
| `bmc/result_parser.{h,cpp}` | Extracts input sequences from Z3 model |
| `bmc/target_spec.h` | `WireConstraint` and `TargetSpec` data structures |
| `solver/z3_solver.{h,cpp}` | Z3 C++ API wrapper (`z3_solve()`) |
| `utils/bitvec_utils.{h,cpp}` | Bit-vector literal helpers |

### Example designs (`examples/`)

| File | Description |
|------|-------------|
| `counter.v` | 4-bit synchronous counter with enable and reset. 16-state space. Smoke test. |
| `lock_fsm.v` | 6-state combination lock FSM with 4-key sequence (3-1-4-1). 15-cycle open timer. |
| `uart_tx.v` | UART transmitter. |
| `uart_tx_fast.v` | UART transmitter, fast-path variant. |

### Multi-module test (`RTL_Tst_Cases/multi_mod/`)

| File | Description |
|------|-------------|
| `sat_counter.v` | 3-bit saturating up/down counter sub-module |
| `dual_counter.v` | Top-level with two `sat_counter` instances, `lock` output |

---

## 12. Algorithm Details

### Campaign loop

```
1. Reset simulation (5 clock cycles).
2. Record initial state (all registers = 0 after reset).
3. Loop:
   a. If a BMC replay is pending:
      - Reset simulation.
      - Step through every input in the BMC sequence.
      - Record every intermediate state (not just the final state).
      - Reset retry counter if any new state was discovered.
   b. Apply one random clock cycle with uniformly random data inputs.
   c. Record the resulting state. Check for full coverage (terminate if yes).
   d. If cycles since last new state ≥ stall_cycles:
      - Ask CoverageDB for an unvisited target.
      - If none: terminate (no_more_targets).
      - If same target has been retried ≥ MAX_TARGET_RETRIES (3): mark exhausted, continue.
      - Invoke BMC binary with the target constraint(s).
      - If SAT: store BmcResult as pending_replay.
      - If UNSAT: mark target exhausted.
4. On timeout: terminate.
```

### Yosys SMT2 model (`clk2fflogic`)

Yosys's `clk2fflogic` pass eliminates the clock port by converting each flip-flop into a pair of SMT2 registers:

- `<reg>#sampled` — the current (stored) value.
- Internal wires — the next-state combinational value.

The pass produces **two SMT2 transitions per RTL clock cycle**: one for the negedge (clk 1→0, combinational propagation) and one for the posedge (clk 0→1, flip-flop capture). `BmcInterface` filters the BMC output to posedge-only steps so that `replay_sequence` maps 1:1 to xsim clock cycles.

### BMC unrolling

The BMC query is an SMT2 formula asserting:

```
init(s_0) ∧ T(s_0, s_1) ∧ T(s_1, s_2) ∧ ... ∧ T(s_{k-1}, s_k) ∧ goal(s_k)
```

where `T` is the design transition function and `goal` constrains the target registers. Iterative deepening increases `k` from 1 to `--max-steps` until SAT or all depths exhausted.

### Target selection

| Phase | Condition | Target produced |
|-------|-----------|----------------|
| Single-register | Some individual values unseen | `{reg: first_unseen_value}` |
| Single-register (exhausted) | Best reg's unseen values all exhausted | Next register with unseen values |
| Joint-state | All individual values covered | `{reg0: v0, reg1: v1, ...}` first unvisited combo |
| Done | All joint states covered or proven unreachable | `None` |

---

## 13. Known Limitations

### `clk2fflogic` model divergence

For designs with complex feedback between internal registers (e.g., `lock_fsm.v`), the Yosys `clk2fflogic` SMT2 model may produce paths that are valid in the model but not reproducible by RTL simulation. This happens when intermediate `$procmux#sampled` registers interact with the negedge half-cycle in ways that don't match the synthesised RTL.

**Mitigation:** `replay_sequence` records all intermediate states, so coverage still progresses even when the BMC's predicted final state is not reached. Targets that consistently fail to replay are marked exhausted after `MAX_TARGET_RETRIES = 3` attempts.

### State space size

The `max_state_space` check only terminates the campaign at `full_coverage` when the space is ≤ 2²⁰ (≈1M states). Larger designs rely on the global timeout. Coverage percentage is still tracked and reported.

### Reset detection

Clock and reset ports are identified by name matching against a built-in list:

- Clocks: `clk`, `clock`, `CLK`, `CLOCK`, `clk_i`, `clk_o`
- Resets: `rst`, `reset`, `rst_n`, `resetn`, `rstn`, `rst_ni`, `areset`, `aresetn`, `nreset`, `sreset`

Designs using non-standard names must rename their ports or patch `design_parser.py`.

### Single-clock designs only

SymbFuzz assumes one clock domain. Multi-clock designs are not supported.

### Simulation backend: xsim vs Verilator

Two backends are available, selectable with `--sim {xsim,verilator}`. Both speak the identical JSON-over-stdio protocol, so every downstream component (orchestrator, corpus store, BMC, mutation) is backend-agnostic and a corpus DB built under one backend is usable by the other.

**xsim (default).** Uses the TCL bridge described above. Works with the Vivado toolchain already required for UVM generation. Per-cycle cost ~20 ms (lock_fsm) after batching.

**Verilator.** Generates a C++ harness (`<out>/verilator/harness.cpp`) and a build script that runs `verilator --cc --exe --build --public-flat-rw`. The compiled binary is spawned and driven over stdin/stdout exactly like xsim's TCL bridge. Observed wall-clock speedups: counter reaches 100 % coverage in **0.0 s** vs xsim's **~3 s**; lock_fsm discovers **~5× more states** in the same 30 s timeout because the cheaper per-cycle cost leaves far more budget for BMC and random exploration.

Limitations and notes:

- Verilator covers a large subset of SystemVerilog but not all (e.g., complex class-based UVM constructs). sv2v preprocessing (`--sv2v`) applies identically to both backends.
- Internal register access uses `--public-flat-rw` with the `<top_module>__DOT__<signal>` naming convention. Register widths are currently clamped to 64 bits in the harness.
- `checkpoint`/`restore` are unsupported on both backends (xsim 2025.2 lacks the TCL command; Verilator equivalent deferred).
- Native Verilator coverage (`--coverage-line/toggle/user`) is not yet wired into `CoverageDB` — deferred to a future phase that will replace the register-tuple metric with branch/toggle coverage.

### Simulator Backends

SymbFuzz ships two interchangeable simulator backends behind an identical JSON-over-stdio protocol:

| Backend | Selected with | Notes |
|---------|--------------|-------|
| Vivado xsim | `--sim xsim` *(default)* | TCL bridge; widely available; slower per cycle on synchronous RTL |
| Verilator ≥ 5.0 | `--sim verilator` | Generated C++ harness compiled via `verilator --cc --build --exe`; ~100× faster per cycle on the smoke tests we've measured |

Both backends are driven by the same `Orchestrator`, `CorpusStore`, `StateForcer`, and `BmcInterface`. Switching is a one-flag change; the same `coverage.db` and `corpus_entries` are reusable across backends (a corpus built under xsim can be restored under Verilator and vice versa).

**Shared JSON protocol** — see the `SimDriver` listing in §9. `step`, `step_trace`, `reset`, `read_state`, `force`, `quit` behave identically; `checkpoint`/`restore` return `{"error":"unsupported"}` on both (a Vivado xsim 2025.2 limitation on that side, deliberately unimplemented on the Verilator side).

**Output directory layout** — xsim drops `tcl_bridge_<m>.tcl` and `compile_<m>.sh` directly under `<module>_symfuzz/`; Verilator puts `harness.cpp`, `compile_<m>.sh`, and `obj_dir/<m>_sim` under `<module>_symfuzz/verilator/`. Both coexist in the same output directory.

**Phase 1 limitations of the Verilator backend**
- Port widths > 64 bits clamp to `uint64_t` in state emission and input routing. Most register-level stimulus fits; designs with wider signals (e.g. some CVA6 internal buses) are not yet supported and should stay on xsim.
- Native Verilator coverage (`--coverage-line/toggle/user`) is not yet wired into `CoverageDB`; register-tuple coverage still applies. This is the planned Phase 2 integration.
- X-propagation follows Verilator's conservative 2-state model. For fully-synchronous designs starting from a clean reset, this is indistinguishable from xsim in practice.

### Unproductive-BMC Retirement

Each BMC call's effectiveness is measured by the *post-replay coverage delta*. When BMC returns SAT but the replay produces no new register-tuple state and no new native-coverage point:

1. **The target is retired immediately** (`mark_target_exhausted`), so the next stall-driven call picks something else.
2. An `unproductive_streak` counter increments. It resets to 0 whenever any BMC replay is productive.
3. When the streak reaches `--bmc-unproductive-limit` (default 5), the campaign terminates with `terminated_by == "no_productive_bmc"`.

This was motivated by an overnight serdiv A/B in which 36 of 57 BMC calls kept retargeting the same register (`res_q=69`) after native coverage saturated — producing no new coverage for ~75 minutes. The new gate ends the campaign within ~5 BMC calls of saturation.

Mutation and random stepping between BMC calls can still reset the streak: any new coverage they produce during the intervening cycles shows up on the next BMC call's productivity check. So the streak only runs when BMC is genuinely the only thing moving — or not moving, as the case may be.

### Stall Model

The stall counter tells the orchestrator when to fall back from random / mutation exploration to BMC-targeted search. Three sources:

| `--stall-source` | Reset trigger | When to use |
|---|---|---|
| `tuple` *(default)* | New unique register-tuple state | Small designs where the state space is finite; legacy behavior. |
| `native` | New native-coverage point | **Required for CVA6-scale designs** — register-tuple states keep growing indefinitely on large designs, preventing the `tuple` source from ever triggering BMC. Native coverage actually saturates and triggers the stall. |
| `any` | Whichever source fired most recently | Most permissive — BMC fires only when neither signal has moved. |

On serdiv, switching from `tuple` to `native` in a 120 s run took BMC from 0 invocations to 8 successful calls and moved native coverage from 76.5 % to 82.4 % — the coverage-driven target loop we built depends on this working correctly.

### Campaign Output Artifacts

Each campaign writes several machine-readable files to `<output_dir>/`:

| File | Shape | Purpose |
|---|---|---|
| `meta.json` | single JSON object | Fully-resolved CLI args + design summary + start timestamp. Campaign reproducibility. |
| `progress.jsonl` | JSON-per-line, one per `--log-every` tick | Periodic snapshot of cycles, native coverage, corpus size, BMC/mutation stats, xcrg/BMC average latency. |
| `bmc.jsonl` | JSON-per-line, one per BMC invocation | Per-call record: `target`, `kind` (`coverage_toggle` vs `register`), `outcome` (`sat`/`unsat`/`timeout`/`error`), `duration_ms`, `depth`. |
| `final_coverage.json` | single JSON object | End-of-campaign snapshot of native-coverage point IDs split into `covered_ids` and `uncovered_ids` with per-kind totals. Diffable across A/B arms. |
| `coverage.db` | SQLite | Full history: `visited_states`, `coverage_log`, `coverage_points`, `coverage_point_universe`, `corpus_entries`. |

### Progress Logging

Every `--log-every` seconds the orchestrator emits a one-line `[prog]` summary to stderr and appends a JSON record to `<output_dir>/progress.jsonl`. Example record:

```json
{"t": 120.1, "cycle": 1340, "cps": 11.2, "states": 578,
 "native_covered": 104, "native_total": 136, "corpus_size": 42,
 "bmc_total": 5, "bmc_ok": 3, "mutations": 71,
 "xcrg_avg_ms": 220, "bmc_avg_ms": 3150}
```

Load the log for offline analysis:

```python
import json
records = [json.loads(l) for l in open("serdiv_symfuzz/progress.jsonl")]
```

Under `--verbose`, individual BMC calls and xcrg polls also emit `[timing]` lines with wall-clock costs, useful for spotting bottleneck shifts mid-campaign.

### Coverage-Driven BMC Targeting

When `--native-coverage` is on, every xcrg poll populates a `coverage_point_universe` table with every instrumented point the simulator knows about (covered + uncovered), alongside the existing `coverage_points` table of hit points. Point IDs carry structured information:

```
xtgl:<module>:<signal>:<bit>:<rise|fall>
```

where `<bit>` is an integer or `*` for "all bits" rows. On each stall event, the orchestrator queries `get_uncovered_coverage_target()` for the next uncovered point (toggles first, line coverage deferred) and translates it into a BMC-compatible register-value constraint:

- Rise on bit K of register R: use `(current_value(R) | (1<<K))` as the BMC goal.
- Fall on bit K of register R: use `(current_value(R) & ~(1<<K))`.
- "All bits" (`bit='*'`): pick bit 0 as a representative. Phase 1b will sweep each bit individually once the BMC binary grows a native `--target-bit` flag.

This over-constrains — we pin the whole register, not just bit K — so some targets UNSAT. The existing `mark_target_exhausted` + retry machinery cycles through the uncovered-point list cheaply until one hits. When register-tuple coverage was blind on large designs, this produces BMC goals that actually correspond to unexecuted RTL behavior.

**xcrg quirk.** xcrg silently discards absolute `-report_dir` values; the driver runs it with `cwd=cov_dir_p` and relative paths. If the expected `report/codeCoverageReport/` directory is missing after a poll, the parser falls back to aggregate-percentage scraping so the campaign never regresses.

**Kill switch.** `--no-coverage-target` bypasses this path; BMC reverts to the legacy `get_unvisited_neighbor_target` picker. Used as an A/B control arm.

**Phase 1b (shipped): native bit-level BMC targets.** The C++ BMC binary accepts `--target-bit <reg>=<bit>=<val>` and emits `(= ((_ extract K K) accessor) (_ bvV 1))` — pinning only the target bit, not the whole register. The orchestrator's coverage-toggle path now produces `{reg: {"bit": K, "val": V}}` targets which the driver forwards as `--target-bit`. This eliminates the Phase 1a over-constraint and drops bit-level BMC solve time from ~1.7 s (legacy full-width) to ~20 ms on lock_fsm — precise targets are usually trivial to SAT. `BmcInterface` auto-detects dict-valued specs and picks the right flag; legacy `{reg: int}` targets still route to `--target`.

**Per-bit expansion of "All bits of X" rows.** xcrg's HTML coverage report reports wide registers as a single "All bits of `<sig>`" row that only toggles green when *every* bit has flipped in both directions. The scraper looks up the signal's width from `DesignInfo.registers` / `inputs` / `outputs` and expands that row into `2*W` per-bit points (`bit=0..W-1` × rise/fall). The BMC loop then sweeps each bit individually via `--target-bit`. Signals whose width can't be resolved (internal xcrg-only wires) keep the legacy `bit='*'` form as a graceful fallback.

**Grey-row stratified sampling.** For xcrg's "Grey" (partial) rows we don't know which specific bits were covered. Rather than emit all `2*W` points (a noise floor that prematurely trips the unproductive-streak gate), the scraper samples `--grey-bit-samples` deterministic bit indices (`range(0, W, max(1, W // grey_bit_samples))`). Indices are stable across polls so retirement persists. Green/Red rows are still expanded fully — their aggregate truth is reliable. `0` disables sampling (full Phase 1a per-bit expansion).

**Class-aware target synthesis (Phase 3).** The coverage-target translator `_toggle_target_to_bmc_target` branches on the signal class:

| Class | Signal location | BMC goal shape | Emitted flag |
|---|---|---|---|
| Flop — top-level or submodule | in `design.registers` (post-flatten, dotted name) | `{sig: {bit, val, flip: True}}` | `--target-flip` |
| Combinational wire / input / output | in `design.inputs` / `design.outputs` / `design.wires` | `{sig: {bit, val}}` | `--target-bit` |
| Unresolvable | not in any of the above | `None` → orchestrator falls back to register-value picker | — |

Verilator's `hier` field (`TOP.<module>.<inst>...`) is stripped of `TOP.` and the top-module prefix to produce a dotted FQ name matching Yosys's flattened `arch_name` convention. Under `--verbose` the classifier logs `[orch] coverage-target classify: sig=<name> bit=<K> → <flop|wire|UNRESOLVED>` on every call.

`DesignInfo.wires` is populated from Yosys's `; yosys-smt2-wire` annotations — a per-design list of every combinational wire the C++ BMC binary can resolve via its 4-tier wire lookup.

**Temporal-flip coverage_toggle targets (Phase 2).** The coverage-driven BMC path emits `--target-flip` for every coverage_toggle goal. Instead of just asking for `bit K = V` at the final state (which BMC trivially satisfies when the bit is already `V`), we require bit `K = V` at step `k` *and* bit `K = (NOT V)` at step `k-1` — forcing a real transition. The SMT2 body is one coupled `and` assertion. Min BMC depth is clamped to 2.

Known observability gap: xcrg's HTML reports per-row Green/Red/Grey aggregates, not per-bit hit counts. When our scraper expands an "All bits of `<sig>`" row into synthetic per-bit point IDs, we can only infer "covered" status when the entire row goes Green. A real bit-level flip hidden inside a still-Grey row *happens* in the simulator but shows `target_hit=False` in `bmc.jsonl` — this is a limitation of xcrg's exposed data, not of the BMC constraint. Resolution would require parsing xsim's binary coverage DB directly (a Xilinx-proprietary format); deferred.

**Strict productivity (`bmc.jsonl`).** Each SAT record carries:

| field | meaning |
|---|---|
| `target_hit` | the targeted coverage point appears in `coverage_points` post-replay (what the coverage reporter says) |
| `direct_hit` | the targeted bit read directly from `driver.read_state()` post-replay is at the requested value and, for flip targets, was at the opposite value pre-replay (*authoritative* for flop-backed targets) |
| `native_delta` | new native points unlocked since the call started |
| `states_delta` | new register-tuple states observed during replay |
| `productive` | for `coverage_toggle`: `direct_hit` when defined, else `target_hit`; for register-fallback: `(native_delta>0 OR states_delta>0)` |

The streak gate uses `productive` directly. When `direct_hit != target_hit`, a `--verbose` diagnostic line points to a coverage-reporter blind spot. When `direct_hit is False` across many calls (as observed on lock_fsm under Verilator), the replay trace doesn't reproduce BMC's SAT witness — a BMC/simulator semantic mismatch rather than a reporter issue. Filter productive vs unproductive calls offline:

```python
import json
calls = [json.loads(l) for l in open("serdiv_armX/bmc.jsonl")]
sat   = [r for r in calls if r["outcome"] == "sat"]
print(sum(r.get("productive", False) for r in sat), "/", len(sat))
```

### Rarity-Weighted Corpus Sampling

`corpus_entries` carries a `contribution_score` column set at insertion time. For native-coverage campaigns, the score equals the number of newly-covered native points at the snapshot that produced the entry; for register-tuple-only campaigns every entry gets score 1 (so sampling is implicitly uniform).

`CorpusStore.sample()` picks proportional to:

```
weight(e) = e.contribution_score / (1 + e.hit_count)
```

This AFL-flavored rule favors entries that unlocked a lot of coverage at once (high contribution) while down-weighting entries that have already been sampled many times (large `hit_count`). All new entries start at `hit_count=0` and immediately compete at full strength. When every score is 1 and every `hit_count` is 0, weights are uniform — this is the compatibility path for small designs and for `--uniform-sampling` campaigns.

**Measured lift.** On lock_fsm with `--native-coverage --mutation-rate 0.2` at matched 60 s timeouts, weighted sampling produced 21 unique states vs 5 for uniform (+320 %) and 75.0 % native coverage vs 61.5 % (+13.5 pts). Use `--uniform-sampling` to re-run as the control arm.

### Native Coverage (A/B Comparison)

With `--native-coverage`, SymbFuzz compiles the harness with the simulator's code-coverage instrumentation and polls current coverage every `--coverage-poll` batches. Results land in a new `coverage_points(point_id, backend, kind, first_seen_cycle, first_seen_time)` SQLite table. The final banner prints the summary alongside the default register-tuple metric so a single run is self-describing for A/B comparison.

| Backend | Compile flag | Runtime flush | Report format | Per-poll cost |
|---------|-------------|---------------|---------------|---------------|
| xsim | `xelab --cc_type st --cov_db_dir … --cov_db_name …` | `write_xsim_coverage` TCL cmd | `xcrg` subprocess stdout scrape | 100–500 ms |
| Verilator | `verilator --coverage-line --coverage-toggle` | `VerilatedCov::write()` | cov.dat key/value records | < 10 ms |

**Verilator cov.dat parser.** Records are of the form `C '\x01<key>\x02<value>\x01<key>\x02<value>...' <count>`. Keys are multi-character (e.g. `f`, `l`, `page`, `o`, `h`). The parser extracts `page` to categorize the record (`v_branch`, `v_toggle`, `v_line`, `v_user`) and builds a stable point ID per category:

| category | point ID shape | meaning |
|---|---|---|
| `v_toggle` | `tgl:<hier>:<signal>[bit]` | per-signal-bit per-direction |
| `v_branch` | `branch:<file>:<line>:<arm>` | `arm` ∈ {`if`, `else`, `elsif`} |
| `v_line` | `line:<file>:<line>:<kind>` | statement / block coverage |
| `v_user` | `user:<hier>:<name>` | user-defined SV cover properties |

Unlike xsim's xcrg HTML (row-aggregate Green/Red/Grey), Verilator's cov.dat exposes individual bit-level hit counts directly — the orchestrator's strict `target_hit` check has a real signal to observe on the Verilator backend.

**Why the numbers disagree across backends.** The two tools count different things at different granularities. xsim `xcrg` reports aggregate percentages per category (statement, toggle, branch, condition, FSM) — SymbFuzz scales each category to 100 synthetic points so that sub-percent deltas across polls translate into new-point deltas. Verilator reports raw instrumented points directly, which on plain Verilog designs skews toward zero because `--coverage-line` produces fewer points than xsim's statement view. **Compare trends within a backend**, not raw numbers across them.

**Overhead tradeoff.** xsim's `xcrg` spawn per poll is the dominant mid-run cost. On lock_fsm with `--coverage-poll 5`, we observed ~3× wall-time slowdown vs `--native-coverage` off. Verilator's in-process flush is essentially free. For long campaigns on xsim, set `--coverage-poll 50` or higher; for Verilator, leave it at the default 10.

**Granularity note for xsim.** Because `xcrg` emits only per-category percentages, SymbFuzz cannot currently show per-line or per-signal hit details for xsim — only aggregate percentages. The generated HTML coverage report under `<output_dir>/xsim_cov/report/` is human-readable and contains full detail; run the campaign, then open `codeCoverageReport/dashboard.html` for drill-down.

### TCL Bridge Batching

The xsim TCL bridge protocol has a `step_trace` command that applies a list of input dicts — one clock posedge each — and returns all resulting states in a single response. The orchestrator drives random exploration, BMC replay, and mutation bursts through this batched path; `CorpusStore.restore` and `StateForcer.replay_sequence` use it too. Collapsing N stdio roundtrips into 1 removes the per-cycle kernel pipe context-switch cost that dominated the previous per-cycle budget on small designs (measured ~2× speedup on `counter`).

Batch size is configurable via `--step-batch` (default 16, clamped to `--stall-cycles`). A batch of 1 reproduces the legacy per-cycle path exactly. Coverage granularity is preserved — the orchestrator still iterates the returned states and records each — only the underlying transport is amortized.

**Error semantics.** A mid-batch simulator error surfaces as `{"states":[...],"error":"<msg>"}` — every successful state up to the failure point is returned, then the driver raises `RuntimeError(err)`. The orchestrator records those pre-error states, so a batch failure never discards observed progress.

**Clock period.** The TCL `run` time unit inside `clk_posedge` defaults to `"1 ps"` and `clk_posedge` uses a single `run` per cycle (no negedge half-cycle). This is safe for single-clock synchronous designs and halves scheduler work per step on large designs. Override with `--clock-period "5 ns"` if the RTL uses `#delays` that require a specific period.

### Corpus & Checkpointing (Phase 1)

SymbFuzz keeps a durable **input-sequence corpus** in `coverage.db` at
`corpus_entries`. Every cycle that produces new coverage also writes a row —
keyed by `state_hash`, containing a run-length-encoded (RLE) list of the
inputs that reach it. The corpus serves two purposes at once:

1. **Campaign resume.** On startup the orchestrator restores the deepest
   corpus entry — `driver.reset()` + replay the RLE sequence — so a killed
   campaign picks up near its previous frontier instead of cold-starting.
2. **AFL-style mutation.** With probability `--mutation-rate` each cycle, the
   orchestrator samples a corpus entry, restores to it, and runs a short
   mutation burst (havoc: fresh random inputs; splice: the tail of another
   entry's sequence). Any new coverage discovered becomes a fresh corpus row
   whose path = seed path + mutation inputs — closing the AFL feedback loop.

**RLE compression.** The orchestrator appends each applied input to
`current_path` via `append_input(segments, inputs)`, which merges runs of
identical inputs into `(inputs_dict, repeat)` segments. BMC-derived paths
compress heavily; random-discovered paths compress little but are stored only
on new-coverage cycles, so total volume stays small (~hundreds of bytes per
entry).

**Design recompilation.** RLE sequences are deterministic replays from
`driver.reset()`, so they keep working even after the RTL is recompiled —
provided the design's semantics haven't changed. A post-restore
`read_state()` check auto-evicts rows whose recorded state_hash no longer
matches what replay produces, so stale entries self-heal without wiping the
store.

**Tier 2 (xsim `.wdb` cache) is currently disabled** because Vivado xsim
2025.2 does not expose a simulator-state checkpoint API (only
`checkpoint_vcd` for waveforms). The `--restore-mode auto` and `xsim`
selections silently downgrade to `replay`. The Tier-2 code paths in
`corpus_store.py` remain in place for future xsim versions or alternative
backends.

**Reset correctness.** The TCL bridge's `reset` handler zeros every data
input before toggling the clock through the deassertion edge — without this,
a stale `en=1` from the previous step could sneak one extra increment into a
"fresh" counter during restore, causing state-hash mismatches.

### SystemVerilog support requires sv2v

Yosys (up to 0.63) cannot parse several SystemVerilog constructs commonly found in industrial designs:

- `parameter type T = logic` — parametric types
- Struct cast literals: `struct_t'(0)`
- `assert ... else $fatal(...)` — elaboration-time fatal assertions

Pass `--sv2v` to convert sources to plain Verilog-2005 before Yosys. The converted file is written to `sv2v_cache/<top>__sv2v.v` and reused for both Yosys analysis and xsim compilation. Install sv2v from [github.com/zachjs/sv2v](https://github.com/zachjs/sv2v).

After sv2v conversion, Yosys's `opt` pass may create internal flip-flops named after source line numbers (e.g., `sv2v_out.v:190$23_Y`). These are automatically filtered from the register list by `design_parser.py` — they never appear in coverage tracking.

### xsim only

The simulation backend is Vivado xsim. Verilator and other simulators would require a new `SimDriver` implementation.
