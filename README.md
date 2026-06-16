# SymbFuzz

SymbFuzz is a coverage-directed RTL fuzzer that combines random simulation with bounded model checking (BMC). It targets hard-to-reach register states and coverage bins using an SMT solver, then replays the witness traces against a simulator to exercise the real design.

The tool is built for **RTL security fuzzing**: finding states and input patterns a random campaign would take millions of cycles to stumble upon. It works on SystemVerilog/Verilog designs, uses Verilator or Xilinx xsim as the simulation backend, and emits native-coverage reports, corpus databases, and optional differential-testing logs.

## Highlights

- **Dual engine** — random/mutation-driven simulation plus a Yosys/Z3 BMC binary that solves for inputs reaching specific register values or coverage bins.
- **Native coverage** — per-bit toggle, line, and branch coverage pulled directly from the simulator, not reconstructed.
- **Corpus and replay** — visited-state corpus with lossless input logs, deterministic replay via checkpoint restore (where supported).
- **Tiered BMC** — shallow fast probes escalate to deep solves only when needed, so the solver's time is spent on the bins that actually need it.
- **Structured stimuli** — pinned-port seed injection (and optional cross-product) drives datapath extremes deterministically.
- **Value-class coverage** — optional secondary metric that tracks how many *distinct operand classes* each bin has been hit under, not just "hit once."
- **Differential modes** — Python predicate plugins for spec-level checks; Verilator↔xsim cross-check for simulator-disagreement hunting.
- **Parallel ablation** — shell runner that pins one campaign per CPU block for multi-arm sweeps on many-core hosts.

## Quickstart

```bash
# 1. Build the BMC binary
cd build && make && cd ..

# 2. Install the Python package (editable)
pip install -e .

# 3. Run a short campaign on a Verilog file
symfuzz --top counter --verilog examples/counter.v --sim verilator \
        --native-coverage --timeout 60 --output-dir counter_out

# 4. Inspect results
tail counter_out/progress.jsonl
```

For longer runs, use a YAML config:

```bash
symfuzz --config my_campaign.yaml
```

## Documentation

| Document | Audience |
|----------|----------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Designers — how the pieces fit, data flow, abstractions |
| [docs/TECHNICAL.md](docs/TECHNICAL.md)       | Maintainers — per-file, per-function reference |
| [docs/USER_MANUAL.md](docs/USER_MANUAL.md)   | Operators — CLI flags, YAML schema, interpreting output |

## Requirements

- Python ≥ 3.10
- Yosys (for the BMC binary's SMT2 backend)
- Z3 (linked by the BMC binary)
- Verilator (default simulator) and/or Xilinx Vivado xsim
- `sv2v` if your design uses SystemVerilog constructs Yosys can't parse

Exact versions, install pointers, and known-good toolchain combinations live in [docs/USER_MANUAL.md](docs/USER_MANUAL.md).

## Known limitations

- Single-clock synchronous designs only.
- Multi-module designs must be flattened to one `--top` module; dependencies are passed as `--verilog` source files.
- xsim does not support in-simulation checkpoints; corpus replay on xsim re-plays the input log from reset each time.
- The BMC binary models the design via `clk2fflogic`; designs that rely on `always @(*)` combinational-loop fixed points may diverge between BMC and simulator.

See [docs/USER_MANUAL.md](docs/USER_MANUAL.md) §"Known limitations" for workarounds.

## License

See [LICENSE](LICENSE) if present; otherwise all rights reserved by the repository owner.
