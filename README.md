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
| Vivado / xsim | 2025.2 | RTL simulation backend |
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
→ {"cmd":"step","inputs":{"en":1,"rst":0}}    ← {"state":{"count":3}}
→ {"cmd":"reset","cycles":5}                   ← {"ok":true}
→ {"cmd":"read_state"}                         ← {"state":{"count":3}}
→ {"cmd":"force","state":{"count":7}}          ← {"ok":true}
→ {"cmd":"quit"}
```

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
| `--target <wire>=<val>` | none | Target constraint (repeatable; ANDed together) |
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
| `sim_driver.py` | Spawns and drives the Vivado xsim subprocess via JSON protocol. |
| `bmc_interface.py` | Calls the `symbfuzz` binary, filters posedge steps from JSON output. |
| `coverage_db.py` | SQLite-backed coverage store with target selection logic. |
| `orchestrator.py` | Main campaign loop: random → stall → BMC → replay → repeat. |
| `state_forcing.py` | Replays a BMC input sequence into the live simulation. |

### Templates (`symfuzz/templates/`)

| File | Generated Output |
|------|-----------------|
| `tcl_bridge.tcl.j2` | `tcl_bridge_<module>.tcl` — xsim JSON bridge |
| `compile.sh.j2` | `compile_<module>.sh` — xvlog + xelab script |
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

### SystemVerilog support requires sv2v

Yosys (up to 0.63) cannot parse several SystemVerilog constructs commonly found in industrial designs:

- `parameter type T = logic` — parametric types
- Struct cast literals: `struct_t'(0)`
- `assert ... else $fatal(...)` — elaboration-time fatal assertions

Pass `--sv2v` to convert sources to plain Verilog-2005 before Yosys. The converted file is written to `sv2v_cache/<top>__sv2v.v` and reused for both Yosys analysis and xsim compilation. Install sv2v from [github.com/zachjs/sv2v](https://github.com/zachjs/sv2v).

After sv2v conversion, Yosys's `opt` pass may create internal flip-flops named after source line numbers (e.g., `sv2v_out.v:190$23_Y`). These are automatically filtered from the register list by `design_parser.py` — they never appear in coverage tracking.

### xsim only

The simulation backend is Vivado xsim. Verilator and other simulators would require a new `SimDriver` implementation.
