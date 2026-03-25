#!/usr/bin/env python3
"""
symfuzz — Coverage-Directed Test Generation for Verilog

Usage:
    symfuzz <verilog_file> --top <module> [options]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Locate the SymbFuzz root (two levels up from this file)
_ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="symfuzz",
        description="Coverage-directed test generation for Verilog using BMC-guided exploration",
    )
    ap.add_argument(
        "verilog", nargs="+",
        help="Verilog/SV source files. Pass dependencies before the top-level "
             "file (e.g. pkg.sv sub.v top.v).  All files are read by Yosys and "
             "compiled by xvlog.",
    )
    ap.add_argument("--top", required=True, help="Top module name")
    ap.add_argument("--output-dir", default=None,
                    help="Directory for generated TB artifacts (default: <module>_symfuzz/)")
    ap.add_argument("--stall-cycles", type=int, default=500,
                    help="Cycles of no coverage improvement before invoking BMC (default: 500)")
    ap.add_argument("--bmc-max-steps", type=int, default=40,
                    help="Max BMC depth per invocation (default: 40)")
    ap.add_argument("--timeout", type=float, default=3600.0,
                    help="Global campaign timeout in seconds (default: 3600)")
    ap.add_argument("--bmc-timeout", type=int, default=30_000,
                    help="Per-BMC-call timeout in milliseconds (default: 30000)")
    ap.add_argument("--no-uvm", action="store_true",
                    help="Skip UVM testbench generation")
    ap.add_argument("--no-compile", action="store_true",
                    help="Skip xvlog/xelab compilation (use existing snapshot)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Enable verbose output")
    ap.add_argument("--gen-only", action="store_true",
                    help="Only generate TB artifacts, do not run simulation")

    args = ap.parse_args(argv)

    # ------------------------------------------------------------------ #
    # Lazy imports (keep startup fast)                                     #
    # ------------------------------------------------------------------ #
    from .bmc_interface   import BmcInterface
    from .coverage_db     import CoverageDB
    from .design_parser   import parse_design
    from .orchestrator    import Orchestrator
    from .sim_driver      import SimDriver
    from .state_forcing   import StateForcer
    from .testbench_gen   import generate_uvm_testbench, generate_vivado_harness

    verilog_files = [str(Path(f).resolve()) for f in args.verilog]
    module_name   = args.top

    output_dir = Path(args.output_dir or f"{module_name}_symfuzz").resolve()

    print(f"[symfuzz] Parsing design: {verilog_files} (top: {module_name})")
    try:
        design = parse_design(verilog_files, top_module=module_name)
    except RuntimeError as e:
        print(f"[symfuzz] ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[symfuzz] Module    : {design.module_name}")
    print(f"[symfuzz] Inputs    : {[p.name for p in design.inputs]}")
    print(f"[symfuzz] Outputs   : {[p.name for p in design.outputs]}")
    print(f"[symfuzz] Registers : {[r.arch_name for r in design.registers]}")
    print(f"[symfuzz] Clock     : {design.clock_port}")
    print(f"[symfuzz] Reset     : {design.reset_port}")
    state_space = design.max_state_space
    space_str   = f"{state_space}" if state_space <= (1 << 20) else f">1M"
    print(f"[symfuzz] State space: {space_str} states")

    # ---- Generate testbench artifacts ----------------------------------
    print(f"[symfuzz] Generating Vivado xsim harness → {output_dir}/")
    tb_dir = generate_vivado_harness(design, output_dir)

    if not args.no_uvm:
        print(f"[symfuzz] Generating UVM testbench → {output_dir}/uvm/")
        generate_uvm_testbench(design, output_dir)

    if args.gen_only:
        print("[symfuzz] --gen-only: done.")
        return

    # ---- Compile -------------------------------------------------------
    symbfuzz_bin = _ROOT / "build" / "symbfuzz"
    if not symbfuzz_bin.exists():
        print(f"[symfuzz] ERROR: BMC binary not found: {symbfuzz_bin}", file=sys.stderr)
        print("[symfuzz]   Run:  cd build && make", file=sys.stderr)
        sys.exit(1)

    driver = SimDriver(design, tb_dir, verbose=args.verbose)

    if not args.no_compile:
        print("[symfuzz] Compiling design (xvlog + xelab) ...")
        try:
            driver.compile()
        except RuntimeError as e:
            print(f"[symfuzz] Compilation failed: {e}", file=sys.stderr)
            sys.exit(1)

    # ---- Set up campaign -----------------------------------------------
    db_path  = output_dir / "coverage.db"
    cov_db   = CoverageDB(db_path, design)
    bmc      = BmcInterface(
        symbfuzz_bin, design,
        max_steps=args.bmc_max_steps,
        timeout_ms=args.bmc_timeout,
        verbose=args.verbose,
    )

    print(f"[symfuzz] Starting campaign (stall={args.stall_cycles} cycles, "
          f"timeout={args.timeout}s)")

    driver.load()
    forcer = StateForcer(driver, design)
    orch   = Orchestrator(
        design, driver, cov_db, bmc, forcer,
        stall_cycles   = args.stall_cycles,
        global_timeout = args.timeout,
        verbose        = args.verbose,
    )

    try:
        result = orch.run()
    finally:
        driver.close()
        cov_db.close()

    # ---- Report --------------------------------------------------------
    print()
    print("=" * 60)
    print(" CAMPAIGN RESULTS")
    print("=" * 60)
    print(f"  Terminated by  : {result.terminated_by}")
    print(f"  Total cycles   : {result.total_cycles}")
    print(f"  Elapsed time   : {result.elapsed_sec:.1f}s")
    print(f"  States visited : {result.total_states} / {space_str}")
    print(f"  Coverage       : {result.coverage_pct:.1f}%")
    print(f"  BMC invocations: {result.bmc_invocations} "
          f"({result.bmc_successes} successful)")
    print(f"  Coverage DB    : {db_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
