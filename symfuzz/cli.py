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


def _parse_bmc_tiers(spec: str):
    """Parse '32:5000,64:15000,128:60000' → [(32,5000),(64,15000),(128,60000)].
    Returns None for 'off' / empty to signal single-tier fallback."""
    if not spec or spec.strip().lower() in ("off", "none", ""):
        return None
    tiers = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        steps_s, ms_s = part.split(":")
        tiers.append((int(steps_s), int(ms_s)))
    return tiers or None


def _write_meta(output_dir: Path, args, design) -> None:
    """
    Dump the fully-resolved campaign configuration and design summary to
    ``<output_dir>/meta.json``. Gives overnight runs a self-describing
    header so we can reproduce them from a glance.
    """
    import datetime, json as _json
    meta = {
        "started_utc": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "args": {k: v for k, v in vars(args).items()
                 if not k.startswith("_")},
        "design": {
            "module":     design.module_name,
            "clock_port": design.clock_port,
            "reset_port": design.reset_port,
            "inputs":     [{"name": p.name, "width": p.width}
                           for p in design.inputs],
            "registers":  [{"arch_name": r.arch_name, "width": r.width}
                           for r in design.registers],
            "verilog_files": design.verilog_files,
        },
    }
    (output_dir / "meta.json").write_text(_json.dumps(meta, indent=2))


def _write_final_coverage(output_dir: Path, cov_db) -> None:
    """
    At campaign end, dump the native-coverage covered/uncovered point-ID
    lists (and per-kind summary) so arms can be diffed post-run without
    running sqlite.
    """
    import json as _json
    conn = cov_db._conn
    uni = [(pid, kind) for pid, kind in conn.execute(
        "SELECT point_id, kind FROM coverage_point_universe")]
    cov = {pid for (pid,) in conn.execute(
        "SELECT point_id FROM coverage_points")}
    covered   = sorted(pid for pid, _ in uni if pid in cov)
    uncovered = sorted(pid for pid, _ in uni if pid not in cov)
    by_kind_total   = {}
    by_kind_covered = {}
    for pid, kind in uni:
        by_kind_total[kind] = by_kind_total.get(kind, 0) + 1
        if pid in cov:
            by_kind_covered[kind] = by_kind_covered.get(kind, 0) + 1
    payload = {
        "total":      len(uni),
        "covered":    len(covered),
        "uncovered":  len(uncovered),
        "by_kind":    {k: {"total": by_kind_total[k],
                           "covered": by_kind_covered.get(k, 0)}
                       for k in by_kind_total},
        "covered_ids":   covered,
        "uncovered_ids": uncovered,
    }
    (output_dir / "final_coverage.json").write_text(_json.dumps(payload, indent=2))


def _apply_yaml_config(ap, args, argv):
    """
    Three-tier precedence: argparse defaults < YAML values < explicit CLI args.

    For each YAML key, overwrite args.<key> only when the user did NOT pass
    that flag on the CLI. Unknown YAML keys raise a clear error.
    """
    if not getattr(args, "config", None):
        return

    import yaml

    cfg_path = Path(args.config)
    try:
        with open(cfg_path) as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[symfuzz] ERROR: failed to load --config {cfg_path}: {e}",
              file=sys.stderr)
        sys.exit(1)
    if not isinstance(raw, dict):
        print(f"[symfuzz] ERROR: --config {cfg_path} must contain a YAML mapping",
              file=sys.stderr)
        sys.exit(1)

    # Determine which flags the user explicitly set on the CLI — any token
    # starting with '-' followed by its name, in argv.
    cli_set: set[str] = set()
    cli_argv = list(argv) if argv is not None else sys.argv[1:]
    for tok in cli_argv:
        if tok.startswith("--"):
            name = tok.split("=", 1)[0][2:].replace("-", "_")
            cli_set.add(name)
        elif tok.startswith("-") and len(tok) > 1 and not tok[1].isdigit():
            cli_set.add(tok[1:].replace("-", "_"))

    valid_keys = set(vars(args).keys())
    for key, value in raw.items():
        norm = key.replace("-", "_")
        if norm not in valid_keys:
            print(f"[symfuzz] ERROR: unknown key '{key}' in --config "
                  f"{cfg_path}", file=sys.stderr)
            sys.exit(1)
        if norm in cli_set:
            continue  # CLI wins
        setattr(args, norm, value)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="symfuzz",
        description="Coverage-directed test generation for Verilog using BMC-guided exploration",
    )
    ap.add_argument(
        "verilog", nargs="*", default=[],
        help="Verilog/SV source files. Pass dependencies before the top-level "
             "file (e.g. pkg.sv sub.v top.v).  All files are read by Yosys and "
             "compiled by xvlog. May also be supplied via --config YAML as the "
             "'verilog' key (list of paths).",
    )
    ap.add_argument("--top", default=None,
                    help="Top module name (required; may be supplied via --config).")
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
    ap.add_argument("--seed-stimuli", dest="seed_stimuli",
                    action=argparse.BooleanOptionalAction, default=True,
                    help="Inject structured extreme-value seeds before the "
                         "main loop (default: on). --no-seed-stimuli disables.")
    ap.add_argument("--seed-cycles", type=int, default=80,
                    help="Cycles per seed stimulus run (default: 80)")
    ap.add_argument("--bmc-target-rarity", dest="bmc_target_rarity",
                    action=argparse.BooleanOptionalAction, default=False,
                    help="Lever 4: prefer bins whose sibling bits are already "
                         "covered over bins in cold/dead regs.")
    ap.add_argument("--bmc-corpus-precheck", dest="bmc_corpus_precheck",
                    action=argparse.BooleanOptionalAction, default=False,
                    help="Lever 5: before invoking BMC, check whether any "
                         "visited state already satisfies the target; if so, "
                         "force it and skip BMC.")
    ap.add_argument("--seed-cross-product", dest="seed_cross_product",
                    action=argparse.BooleanOptionalAction, default=False,
                    help="Lever 6: emit cross-product seed stimuli over pairs "
                         "of data-input extremes (can be expensive).")
    ap.add_argument("--bmc-witness-mutations", type=int, default=0,
                    help="Lever 7: after a successful BMC replay, replay N "
                         "mutated variants of the witness (default: 0 = off)")
    ap.add_argument("--bmc-per-reg-budget", type=int, default=0,
                    help="Lever 8: max BMC attempts per arch_name register "
                         "before its bins are retired (default: 0 = unlimited)")
    ap.add_argument("--value-class-coverage", dest="value_class_coverage",
                    action=argparse.BooleanOptionalAction, default=False,
                    help="Phase 1: record value-class signatures per "
                         "covered bin; report diversification in [prog].")
    ap.add_argument("--value-class-target", type=int, default=4,
                    help="Phase 1/2: distinct signatures per bin required "
                         "for diversification (default: 4)")
    ap.add_argument("--value-class-stall", dest="value_class_stall",
                    action=argparse.BooleanOptionalAction, default=False,
                    help="Phase 2: use diversification growth as the stall "
                         "signal (equivalent to --stall-source value_class).")
    ap.add_argument("--value-class-bias", dest="value_class_bias",
                    action=argparse.BooleanOptionalAction, default=False,
                    help="Phase 3 (weak): periodically inject seed bursts "
                         "targeting missing value-class signatures on an "
                         "under-diversified bin.")
    ap.add_argument("--value-class-bias-every", type=int, default=2000,
                    help="Run the diversification bias pass every N cycles "
                         "(default: 2000)")
    ap.add_argument("--predicate-module", type=str, default=None,
                    help="Differential mode 1: path to a Python file "
                         "exposing check(state, inputs) -> bool | None. "
                         "Violations are logged to violations.jsonl.")
    ap.add_argument("--differential-cross-sim", dest="differential_cross_sim",
                    action=argparse.BooleanOptionalAction, default=False,
                    help="Differential mode 3: mirror every step to a second "
                         "simulator backend (Verilator <-> xsim) and log any "
                         "state divergence to differential.jsonl.")
    ap.add_argument("--bmc-depth-tiers", type=str, default="32:5000,64:15000,128:60000",
                    help="Tiered (steps:ms) budgets for coverage-toggle BMC, "
                         "comma-separated. Cheap tiers run first; promotion "
                         "on timeout/UNSAT. Set to 'off' to disable tiering. "
                         "(default: 32:5000,64:15000,128:60000)")
    ap.add_argument("--no-uvm", action="store_true",
                    help="Skip UVM testbench generation")
    ap.add_argument("--no-compile", action="store_true",
                    help="Skip xvlog/xelab compilation (use existing snapshot)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Enable verbose output")
    ap.add_argument("--gen-only", action="store_true",
                    help="Only generate TB artifacts, do not run simulation")
    ap.add_argument("--sv2v", action="store_true",
                    help="Preprocess sources with sv2v before Yosys. Required for "
                         "designs that use SV constructs Yosys cannot parse "
                         "(parameter type, struct casts, complex package functions). "
                         "sv2v must be installed: https://github.com/zachjs/sv2v")
    ap.add_argument("--restore-mode", choices=("auto", "replay", "xsim", "off"),
                    default="auto",
                    help="Checkpoint / corpus tier selection. "
                         "'auto' (default): prefer xsim .wdb cache, fall back to "
                         "RLE replay, rebuild .wdb on fallback. "
                         "'replay': only RLE replay, no .wdb on disk. "
                         "'xsim': prefer .wdb, fall back but don't rebuild. "
                         "'off': disable checkpointing/corpus entirely.")
    ap.add_argument("--wdb-min-depth", type=int, default=20,
                    help="Skip .wdb caching for sequences shorter than this (default: 20)")
    ap.add_argument("--wdb-budget-mb", type=int, default=500,
                    help="LRU disk budget for .wdb cache in MB (default: 500)")
    ap.add_argument("--mutation-rate", type=float, default=0.1,
                    help="Probability per cycle of running an AFL-style mutation "
                         "burst from a sampled corpus entry (default: 0.1; 0 disables)")
    ap.add_argument("--mutation-burst", type=int, default=20,
                    help="Max cycles per mutation burst (default: 20)")
    ap.add_argument("--step-batch", type=int, default=16,
                    help="Cycles per batched step_trace call. 1 reproduces the "
                         "legacy single-step path. Clamped to stall_cycles. "
                         "(default: 16)")
    ap.add_argument("--clock-period", default="1 ps",
                    help="TCL 'run' time unit inside clk_posedge (default: "
                         "'1 ps'). Set higher (e.g. '5 ns') if your RTL uses "
                         "non-zero #delays that depend on clock period.")
    ap.add_argument("--bmc-unproductive-limit", type=int, default=5,
                    help="Terminate the campaign after N consecutive BMC "
                         "calls that returned SAT but added no new "
                         "coverage. The unproductive target is retired "
                         "immediately on each such call. 0 disables "
                         "(legacy behaviour: retry indefinitely). "
                         "Default: 5.")
    ap.add_argument("--stall-source", choices=("tuple", "native", "any"),
                    default="tuple",
                    help="What resets the stall counter. 'tuple' (default): "
                         "new register-tuple states (legacy). 'native': new "
                         "native-coverage points (requires --native-coverage). "
                         "'any': whichever is more recent.")
    ap.add_argument("--log-every", type=float, default=30.0,
                    help="Seconds between [prog] progress lines written to "
                         "stderr (default 30). 0 disables.")
    ap.add_argument("--progress-log", default=None,
                    help="Path to a JSONL progress log. Default: "
                         "<output_dir>/progress.jsonl. Set empty string to "
                         "disable file output (stderr lines remain).")
    ap.add_argument("--no-coverage-target", action="store_true",
                    help="Disable coverage-driven BMC targeting. BMC falls "
                         "back to the legacy register-value target picker. "
                         "Used as an A/B control arm.")
    ap.add_argument("--uniform-sampling", action="store_true",
                    help="Force uniform corpus sampling (disables the "
                         "rarity-weighted sampler). Primarily an A/B "
                         "control switch.")
    ap.add_argument("--native-coverage", action="store_true",
                    help="Enable simulator-native line + toggle coverage. "
                         "Compiled into the harness on first build; polled "
                         "mid-run. Used for A/B comparison between xsim and "
                         "verilator.")
    ap.add_argument("--coverage-poll", type=int, default=10,
                    help="Poll native coverage every N batches when "
                         "--native-coverage is on (default: 10).")
    ap.add_argument("--grey-bit-samples", type=int, default=4,
                    help="For xcrg Grey-classified 'All bits of <sig>' rows, "
                         "emit only this many stratified per-bit coverage "
                         "points (instead of all W). Reduces false-uncovered "
                         "noise that prematurely trips the unproductive "
                         "streak. 0 = full per-bit expansion. Default: 4.")
    ap.add_argument("--verilator-threads", type=int, default=1,
                    help="Number of worker threads for Verilator's multi-"
                         "threaded simulation model (-j equivalent in "
                         "compile; --threads at runtime). 1 = single-"
                         "threaded (default). Benefits designs with enough "
                         "parallel combinational logic; small designs may "
                         "regress due to scheduling overhead. Ignored for "
                         "--sim xsim.")
    ap.add_argument("--sim", choices=("xsim", "verilator"), default="xsim",
                    help="Simulation backend. 'xsim' (default) uses Vivado xsim + "
                         "the TCL bridge. 'verilator' compiles a Verilator C++ "
                         "harness and drives it via the same JSON protocol — "
                         "typically far faster per cycle on synchronous RTL.")
    ap.add_argument("--config", default=None,
                    help="YAML file supplying default values for any flag. "
                         "CLI arguments override file values; file values "
                         "override argparse defaults. Keys use underscores in "
                         "place of dashes (e.g. 'stall_cycles').")

    args = ap.parse_args(argv)
    _apply_yaml_config(ap, args, argv)

    if not args.verilog:
        print("[symfuzz] ERROR: no Verilog files provided (positional args or "
              "--config 'verilog' key required).", file=sys.stderr)
        sys.exit(2)
    if not args.top:
        print("[symfuzz] ERROR: --top is required (or 'top' key in --config).",
              file=sys.stderr)
        sys.exit(2)
    if args.stall_source == "native" and not args.native_coverage:
        print("[symfuzz] ERROR: --stall-source native requires "
              "--native-coverage to be enabled.", file=sys.stderr)
        sys.exit(2)

    # ------------------------------------------------------------------ #
    # Lazy imports (keep startup fast)                                     #
    # ------------------------------------------------------------------ #
    from .bmc_interface   import BmcInterface
    from .corpus_store    import CorpusStore
    from .coverage_db     import CoverageDB
    from .design_parser   import parse_design
    from .drivers         import make_driver
    from .orchestrator    import Orchestrator
    from .state_forcing   import StateForcer
    from .testbench_gen   import (generate_uvm_testbench,
                                  generate_verilator_harness,
                                  generate_vivado_harness)

    verilog_files = [str(Path(f).resolve()) for f in args.verilog]
    module_name   = args.top

    output_dir = Path(args.output_dir or f"{module_name}_symfuzz").resolve()

    if args.sv2v:
        print("[symfuzz] sv2v preprocessing enabled")
    print(f"[symfuzz] Parsing design: {verilog_files} (top: {module_name})")
    try:
        design = parse_design(verilog_files, top_module=module_name, use_sv2v=args.sv2v)
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
    if args.sim == "verilator":
        print(f"[symfuzz] Generating Verilator harness → {output_dir}/verilator/")
        generate_verilator_harness(design, output_dir,
                                   native_coverage=args.native_coverage,
                                   verilator_threads=args.verilator_threads)
        generate_vivado_harness(design, output_dir,
                                clock_period=args.clock_period,
                                native_coverage=args.native_coverage)
        tb_dir = output_dir
    else:
        print(f"[symfuzz] Generating Vivado xsim harness → {output_dir}/")
        tb_dir = generate_vivado_harness(design, output_dir,
                                         clock_period=args.clock_period,
                                         native_coverage=args.native_coverage)

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

    driver = make_driver(args.sim, design, tb_dir, verbose=args.verbose,
                         grey_bit_samples=args.grey_bit_samples)

    if not args.no_compile:
        if args.sim == "verilator":
            print("[symfuzz] Compiling design (verilator) ...")
        else:
            print("[symfuzz] Compiling design (xvlog + xelab) ...")
        try:
            driver.compile()
        except RuntimeError as e:
            print(f"[symfuzz] Compilation failed: {e}", file=sys.stderr)
            sys.exit(1)

    # Differential mode 3: build & compile the cross-sim driver into its own
    # subdir so it doesn't collide with the primary driver's artefacts.
    cross_driver = None
    if args.differential_cross_sim:
        cross_sim = "xsim" if args.sim == "verilator" else "verilator"
        cross_tb  = output_dir / f"cross_{cross_sim}"
        cross_tb.mkdir(exist_ok=True, parents=True)
        print(f"[symfuzz] Differential cross-sim: spawning {cross_sim}")
        cross_driver = make_driver(cross_sim, design, cross_tb,
                                   verbose=args.verbose,
                                   grey_bit_samples=args.grey_bit_samples)
        try:
            cross_driver.compile()
        except RuntimeError as e:
            print(f"[symfuzz] Cross-sim compile failed: {e}", file=sys.stderr)
            cross_driver = None

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

    # ---- Meta dump for reproducibility ---------------------------------
    _write_meta(output_dir, args, design)

    # Open per-BMC call log (one JSON record per invocation).
    bmc.open_log(str(output_dir / "bmc.jsonl"))

    driver.load()
    forcer = StateForcer(driver, design)

    # ---- Corpus / checkpoint store -------------------------------------
    corpus: "CorpusStore | None" = None
    if args.restore_mode != "off":
        snapshot_file = tb_dir / "xsim.dir" / f"{design.module_name}_sim" / "xsim.rdi"
        corpus = CorpusStore(
            db_path       = db_path,
            wdb_dir       = output_dir / "wdb",
            design        = design,
            snapshot_file = snapshot_file,
            mode          = args.restore_mode,
            wdb_min_depth = args.wdb_min_depth,
            wdb_budget_mb = args.wdb_budget_mb,
            verbose       = args.verbose,
        )

    orch   = Orchestrator(
        design, driver, cov_db, bmc, forcer,
        stall_cycles   = args.stall_cycles,
        global_timeout = args.timeout,
        verbose        = args.verbose,
        corpus_store   = corpus,
        mutation_rate  = args.mutation_rate,
        mutation_burst = args.mutation_burst,
        step_batch     = args.step_batch,
        native_coverage = args.native_coverage,
        coverage_poll  = args.coverage_poll,
        uniform_sampling = args.uniform_sampling,
        coverage_target  = not args.no_coverage_target,
        stall_source     = args.stall_source,
        log_every        = args.log_every,
        progress_log     = (args.progress_log
                            if args.progress_log is not None
                            else str(output_dir / "progress.jsonl")),
        bmc_unproductive_limit = args.bmc_unproductive_limit,
        bmc_depth_tiers        = _parse_bmc_tiers(args.bmc_depth_tiers),
        seed_stimuli           = args.seed_stimuli,
        seed_cycles            = args.seed_cycles,
        bmc_target_rarity      = args.bmc_target_rarity,
        bmc_corpus_precheck    = args.bmc_corpus_precheck,
        seed_cross_product     = args.seed_cross_product,
        bmc_witness_mutations  = args.bmc_witness_mutations,
        bmc_per_reg_budget     = args.bmc_per_reg_budget,
        value_class_coverage   = args.value_class_coverage,
        value_class_target     = args.value_class_target,
        value_class_stall      = args.value_class_stall,
        value_class_bias       = args.value_class_bias,
        value_class_bias_every = args.value_class_bias_every,
        predicate_module       = args.predicate_module,
        differential_cross_sim = args.differential_cross_sim,
    )

    native_summary = None
    try:
        if cross_driver is not None:
            orch.cross_driver = cross_driver
        result = orch.run()
        if args.native_coverage:
            native_summary = cov_db.native_summary()
            _write_final_coverage(output_dir, cov_db)
    finally:
        bmc.close_log()
        driver.close()
        cov_db.close()
        if corpus is not None:
            corpus.close()

    # ---- Report --------------------------------------------------------
    print()
    print("=" * 60)
    print(" CAMPAIGN RESULTS")
    print("=" * 60)
    print(f"  Backend        : {args.sim}")
    print(f"  Terminated by  : {result.terminated_by}")
    print(f"  Total cycles   : {result.total_cycles}")
    print(f"  Elapsed time   : {result.elapsed_sec:.1f}s")
    print(f"  States visited : {result.total_states} / {space_str}")
    print(f"  Coverage       : {result.coverage_pct:.1f}%")
    if args.native_coverage and native_summary is not None:
        print(f"  Native coverage: {native_summary}")
    print(f"  BMC invocations: {result.bmc_invocations} "
          f"({result.bmc_successes} successful)")
    print(f"  Coverage DB    : {db_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
