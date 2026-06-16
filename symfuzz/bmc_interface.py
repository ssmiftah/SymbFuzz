"""
bmc_interface.py — Subprocess wrapper around the compiled symbfuzz BMC binary.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .design_parser import DesignInfo


@dataclass
class BmcResult:
    depth: int
    steps: list[dict[str, int]]     # steps[i] = {port_name: value} at BMC step i
    target: dict[str, int]          # the requested target constraints


class BmcInterface:
    def __init__(
        self,
        symbfuzz_binary: str | Path,
        design: DesignInfo,
        max_steps: int = 40,
        timeout_ms: int = 30_000,
        verbose: bool = False,
    ):
        self.binary  = str(Path(symbfuzz_binary).resolve())
        self.design  = design
        self.max_steps  = max_steps
        self.timeout_ms = timeout_ms
        self.verbose    = verbose
        # Wall-clock stats for the progress logger.
        self.last_duration_ms: float = 0.0
        self.total_duration_ms: float = 0.0
        self.total_calls: int = 0
        # Append-only per-call log. Set via open_log(); closed via close_log().
        self._log_fh = None
        # Cached libz3.so directory (None means "lookup not yet attempted").
        self._z3_lib_dir: Optional[str] = None

    @staticmethod
    def _detect_z3_lib_dir() -> Optional[str]:
        """Locate a directory containing libz3.so. Tries (in order):
          1. SYMFUZZ_Z3_LIB env var (explicit override).
          2. The pip-installed z3-solver package bundled with this venv.
          3. ldconfig cache.
        Returns None if libz3 is already resolvable by the system loader."""
        override = os.environ.get("SYMFUZZ_Z3_LIB")
        if override and Path(override, "libz3.so").exists():
            return override
        try:
            import z3 as _z3
            cand = Path(_z3.__file__).parent / "lib"
            if (cand / "libz3.so").exists():
                return str(cand)
        except Exception:
            pass
        return None

    def _find_z3_lib_dir(self) -> Optional[str]:
        if self._z3_lib_dir is None:
            self._z3_lib_dir = self._detect_z3_lib_dir() or ""
        return self._z3_lib_dir or None

    def open_log(self, path: str) -> None:
        """Open an append-only JSONL log that records every BMC invocation."""
        self._log_fh = open(path, "a", buffering=1)

    def close_log(self) -> None:
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None

    def _log_record(self, rec: dict) -> None:
        if self._log_fh is None:
            return
        try:
            self._log_fh.write(json.dumps(rec) + "\n")
        except Exception:
            pass

    def find_sequence(
        self,
        targets: dict[str, int],
        kind: str = "register",
        max_steps: Optional[int] = None,
        timeout_ms: Optional[int] = None,
    ) -> Optional[BmcResult]:
        """
        Ask BMC to find an input sequence driving the design to *targets*
        (a dict of {arch_name: desired_value}).
        Returns ``None`` if no path is found within *max_steps*.
        """
        # Pass all source files (dependencies first) so Yosys can resolve
        # multi-module designs.  Fall back to verilog_path for single-file cases.
        src_files = self.design.verilog_files or [self.design.verilog_path]
        eff_max_steps  = self.max_steps  if max_steps  is None else max_steps
        eff_timeout_ms = self.timeout_ms if timeout_ms is None else timeout_ms
        cmd = [
            self.binary,
            *src_files,
            "--top",    self.design.module_name,
            "--output", "json",
            "--max-steps", str(eff_max_steps),
            "--timeout",   str(eff_timeout_ms),
        ]
        for reg_name, spec in targets.items():
            if isinstance(spec, dict) and spec.get("flip"):
                # Temporal-flip goal: bit K equals V at step k, NOT V at k-1.
                cmd += ["--target-flip",
                        f"{reg_name}={int(spec['bit'])}={int(spec['val'])}"]
            elif isinstance(spec, dict):
                # Bit-level equality goal: bit K equals V at step k.
                cmd += ["--target-bit",
                        f"{reg_name}={int(spec['bit'])}={int(spec['val'])}"]
            else:
                cmd += ["--target", f"{reg_name}={int(spec)}"]

        if self.verbose:
            print(f"[bmc] {' '.join(cmd)}")

        # The BMC binary dynamically links libz3.so. If it's not on the
        # system loader path, look it up in the pip-installed z3-solver
        # package and prepend its directory to LD_LIBRARY_PATH for this
        # subprocess only. Cached on the instance after the first hit.
        env = os.environ.copy()
        z3_lib = self._find_z3_lib_dir()
        if z3_lib:
            existing = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = (z3_lib + ":" + existing) if existing else z3_lib

        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                # Kill buffer scales with the budget: 20% of timeout, clamped
                # to [2s, 10s]. Short tier-0 budgets would otherwise get a
                # flat +10s that dominates actual solve time.
                timeout=eff_timeout_ms / 1000 + max(2.0, min(10.0, eff_timeout_ms / 1000 * 0.2)),
            )
        except subprocess.TimeoutExpired:
            self._record_timing(t0, "timeout", targets)
            self._log_record({"t": time.monotonic(), "outcome": "timeout",
                              "target": targets, "kind": kind,
                              "duration_ms": round(self.last_duration_ms, 1),
                              "depth": None})
            if self.verbose:
                print("[bmc] timed out")
            return None
        dur_ms = (time.monotonic() - t0) * 1000.0
        self.last_duration_ms = dur_ms
        self.total_duration_ms += dur_ms
        self.total_calls += 1

        # BMC binary: exit 0 = sat, exit 2 = unsat/not found
        if proc.returncode == 2:
            self._log_record({"t": time.monotonic(), "outcome": "unsat",
                              "target": targets, "kind": kind,
                              "duration_ms": round(dur_ms, 1),
                              "depth": None})
            if self.verbose:
                print(f"[timing] bmc find_sequence({targets}) "
                      f"{dur_ms/1000.0:.2f}s UNSAT")
                print(f"[bmc] no path to {targets}")
            return None

        if proc.returncode != 0:
            self._log_record({"t": time.monotonic(), "outcome": "error",
                              "target": targets, "kind": kind,
                              "duration_ms": round(dur_ms, 1),
                              "rc": proc.returncode,
                              "depth": None})
            if self.verbose:
                print(f"[bmc] error (rc={proc.returncode}):\n{proc.stderr}")
            return None

        # Parse JSON from stdout
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            if self.verbose:
                print(f"[bmc] JSON parse error: {e}\nstdout: {proc.stdout[:200]}")
            return None

        steps = data.get("steps", [])

        # Return the full step list *including* clk. The orchestrator
        # replays these via driver.step_raw_trace, which drives the clock
        # explicitly from each step's inputs — preserving clk2fflogic's
        # alternating negedge/posedge half-cycle semantics exactly as BMC
        # saw them. (Previously we filtered to posedge-only, which dropped
        # the negedge inputs BMC's SAT witness depended on — see Phase 4
        # full-trace replay plan.)
        clean_steps = list(steps)

        depth_val = data.get("depth", len(steps))
        # SAT records are written by the orchestrator after the post-replay
        # productivity check (see Orchestrator.run). The BmcInterface only
        # logs non-productive outcomes (UNSAT / timeout / error) since those
        # have no replay to attach productivity data to.
        if self.verbose:
            print(f"[timing] bmc find_sequence({targets}) "
                  f"{dur_ms/1000.0:.2f}s SAT depth={depth_val}")

        return BmcResult(
            depth=data.get("depth", len(steps)),
            steps=clean_steps,
            target=targets,
        )

    def _record_timing(self, t0: float, tag: str, targets) -> None:
        """Accumulate a failed/aborted BMC timing for the progress log."""
        dur_ms = (time.monotonic() - t0) * 1000.0
        self.last_duration_ms = dur_ms
        self.total_duration_ms += dur_ms
        self.total_calls += 1
        if self.verbose:
            print(f"[timing] bmc find_sequence({targets}) "
                  f"{dur_ms/1000.0:.2f}s {tag}")
