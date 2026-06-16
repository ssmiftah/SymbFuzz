# SymbFuzz — File Outline

Files that constitute the SymbFuzz tool itself. Run-output directories,
target RTL, per-experiment configs, and experiment runner scripts are
excluded — see [Not part of the tool](#not-part-of-the-tool) below.

## Project root

| Path | Role |
|---|---|
| `CMakeLists.txt` | Build configuration for the C++ BMC binary |
| `pyproject.toml` | Python package metadata and entry points |
| `symfuzz_run.py` | Top-level launcher script |
| `README.md` | Project overview |

## Python orchestrator — `symfuzz/`

Top-level fuzz engine. The CLI in `cli.py` is the user-facing entry; everything else is consumed by it.

| File | Role |
|---|---|
| `cli.py` | Command-line interface and YAML config loader |
| `orchestrator.py` | Main fuzz loop: random + BMC + mutation cadence, coverage polling |
| `bmc_interface.py` | Subprocess wrapper around the compiled C++ BMC binary |
| `coverage_db.py` | SQLite coverage tracking, BMC target picker, dead-signal filter |
| `corpus_store.py` | Corpus / checkpoint store, weighted replay sampling |
| `design_parser.py` | Yosys + sv2v invocation, SMT2 annotation parsing, register/wire extraction |
| `input_bias.py` | Coverage-driven adaptive input biasing model |
| `mutations.py` | Mutation strategies for corpus replay |
| `sim_driver.py` | Driver loader (selects Verilator / xsim) |
| `state_forcing.py` | State-forcing helpers used by BMC replay |
| `testbench_gen.py` | Generates per-design testbench code (Verilator harness + UVM) |
| `value_classes.py` | Value-class signature taxonomy for diversification coverage |
| `__init__.py` | Package init |

### Backend drivers — `symfuzz/drivers/`

JSON-over-stdio protocol shared by both backends.

| File | Role |
|---|---|
| `base.py` | Common stdio driver (step / reset / read_state / force / coverage); register-validity probe |
| `verilator.py` | Verilator backend — compile, load, coverage parse |
| `xsim.py` | Vivado xsim backend |
| `__init__.py` | Package init |

### Code-generation templates — `symfuzz/templates/`

Jinja2 templates rendered into per-design testbench code by `testbench_gen.py`.

| File | Role |
|---|---|
| `verilator_harness.cpp.j2` | Verilator C++ harness with stdio command loop |
| `verilator_compile.sh.j2` | Verilator compile script |
| `compile.sh.j2` | xsim compile script |
| `tcl_bridge.tcl.j2` | xsim TCL command bridge |
| `uvm_tb_top.sv.j2` | UVM testbench top |
| `uvm_env.sv.j2`, `uvm_test.sv.j2` | UVM environment / test |
| `uvm_agent.sv.j2`, `uvm_driver.sv.j2`, `uvm_monitor.sv.j2` | UVM agent / driver / monitor |
| `uvm_if.sv.j2`, `uvm_seq_item.sv.j2` | UVM interface / sequence item |
| `uvm_rand_seq.sv.j2`, `uvm_bmc_seq.sv.j2` | UVM random / BMC-replay sequences |

## C++ BMC backend — `src/` + `include/`

Compiled by CMake into the `build/symfuzz` binary that `bmc_interface.py` invokes.

| Path | Role |
|---|---|
| `src/main.cpp` | CLI entry point for the BMC binary |
| `src/frontend/yosys_driver.cpp` | Runs Yosys, emits SMT2 |
| `src/frontend/smt2_parser.cpp` | Parses Yosys SMT2 output |
| `src/bmc/bmc_engine.cpp` | BMC unrolling, target encoding, search loop |
| `src/bmc/smt2_builder.cpp` | Builds the SMT2 transition relation per BMC depth |
| `src/bmc/result_parser.cpp` | Decodes the SAT witness back into a step sequence |
| `src/solver/z3_solver.cpp` | Z3 backend wrapper |
| `src/utils/bitvec_utils.cpp` | Bit-vector helpers |
| `include/frontend/{yosys_driver,smt2_parser}.h` | Frontend headers |
| `include/bmc/{bmc_engine,smt2_builder,result_parser,target_spec}.h` | BMC headers |
| `include/solver/z3_solver.h` | Solver header |
| `include/utils/bitvec_utils.h` | Utils header |

## Documentation — `docs/`

| File | Role |
|---|---|
| `ARCHITECTURE.md` | High-level architecture overview |
| `TECHNICAL.md` | Technical deep-dive (BMC encoding, coverage, protocols) |
| `USER_MANUAL.md` | User guide (CLI flags, YAML schema, examples) |
| `FILES.md` | This file |

## Design adapters — `cva6_wrappers/`

SystemVerilog wrappers around CVA6 submodules that hardcode struct widths
and flatten parameterized config types into plain ports. Required because
Yosys + sv2v can't elaborate CVA6's `parameter type` patterns directly.
These are per-design adapter code, not tool engine code, but are
maintained as part of this repository.

| File | Adapts |
|---|---|
| `alu_wrap.sv` | CVA6 ALU |
| `branch_unit_wrap.sv` | CVA6 branch unit |
| `commit_stage_wrap.sv` | CVA6 commit stage |
| `csr_regfile_wrap.sv` | CVA6 CSR register file |
| `decoder_wrap.sv` | CVA6 instruction decoder |

## Example predicates — `example_predicates/`

User-facing examples of differential-mode predicate plugins.

| File | Role |
|---|---|
| `serdiv_checks.py` | Example `check(state, inputs)` plugin for serdiv |

---

## Packaging for redistribution

If you tar / push the tool to someone else, this is the recommended file
set. Total size is ~1 MB instead of the ~60 MB+ you'd get if you
included everything.

### Include

| Path | Why |
|---|---|
| `symfuzz/` | Python orchestrator (all sub-packages, templates) |
| `src/`, `include/` | C++ BMC backend source |
| `CMakeLists.txt` | Builds the BMC binary |
| `pyproject.toml` | Python package metadata, installs `symfuzz` CLI |
| `symfuzz_run.py` | Top-level launcher |
| `README.md`, `docs/` | All four markdown docs |
| `examples/` | Small demo RTL (counter, lock_fsm, uart_tx) — runnable out of the box |
| `example_predicates/` | Example predicate plugin for `--predicate-module` |
| `cva6_wrappers/` | Optional — include only if the recipient targets CVA6 |

### Exclude

| Path | Why |
|---|---|
| `RTL_Tst_Cases/` | 60 MB of third-party RTL (CVA6, serdiv, …) — recipients should clone their own source repos |
| Run-output directories (`*_armA/`, `cva6_*_long/`, `*_smoke/`, `counter_symfuzz/`, `lock_fsm_symfuzz/`, `manual_test/`) | Contain `coverage.db`, `bmc.jsonl`, etc.; large and recipient-specific |
| Per-arm YAML configs (`serdiv_armA-H5.yaml`, `cva6_csr_regfile_2h_*.yaml`, etc.) | Experimental records, not tool code |
| Per-arm shell scripts (`run_armC.sh`, `run_overnight_ab.sh`, etc.) | Experiment runners — recipient will write their own |
| `build/`, `sv2v_cache/`, `symfuzz.egg-info/`, `.venv*/` | Build and cache artifacts |
| `xsim.dir/`, `xsim_cov/`, `xelab.pb`, `xvlog.log` | xsim run artifacts |
| `backups/` | Local backups |

### Tar recipe

A one-shot tarball that produces the right set:

```sh
tar -czf symfuzz.tar.gz \
    symfuzz/ src/ include/ docs/ examples/ \
    example_predicates/ cva6_wrappers/ \
    CMakeLists.txt pyproject.toml symfuzz_run.py README.md \
    --exclude='__pycache__' --exclude='*.pyc'
```

Recipient unpacks, then follows `docs/USER_MANUAL.md` § Installation.

---

## Not part of the tool

For reference, the following are NOT engine code and are deliberately
omitted above:

- **Target RTL**: `RTL_Tst_Cases/` (CVA6 sources, serdiv, etc.)
- **Run-output directories**: any `*_armA/`, `cva6_*_long/`, `*_smoke/`,
  `counter_symfuzz/`, `lock_fsm_symfuzz/`, `manual_test/`, `xsim.dir/`,
  `xsim_cov/`, etc. — contain `coverage.db`, `bmc.jsonl`, `run.log`,
  `progress.jsonl`, generated `verilator/` / `uvm/` subtrees
- **Per-experiment YAML configs**: `cva6_*.yaml`, `serdiv_*.yaml` —
  drive the tool, but are per-design / per-arm configuration rather
  than tool engine code
- **Experiment-runner shell scripts**: `run_armC.sh`, `run_armE.sh`,
  `run_overnight_ab.sh`, `run_cva6_parallel.sh`, etc.
- **Build / cache artifacts**: `build/`, `sv2v_cache/`, `symfuzz.egg-info/`,
  `.venv/`, `.venv_broken_*/`, `xelab.pb`, `xvlog.log`, `backups/`
- **Examples directory**: `examples/`
