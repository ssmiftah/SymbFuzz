"""
bmc_interface.py — Subprocess wrapper around the compiled symbfuzz BMC binary.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
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

    def find_sequence(
        self,
        targets: dict[str, int],
    ) -> Optional[BmcResult]:
        """
        Ask BMC to find an input sequence driving the design to *targets*
        (a dict of {arch_name: desired_value}).
        Returns ``None`` if no path is found within *max_steps*.
        """
        # Pass all source files (dependencies first) so Yosys can resolve
        # multi-module designs.  Fall back to verilog_path for single-file cases.
        src_files = self.design.verilog_files or [self.design.verilog_path]
        cmd = [
            self.binary,
            *src_files,
            "--top",    self.design.module_name,
            "--output", "json",
            "--max-steps", str(self.max_steps),
            "--timeout",   str(self.timeout_ms),
        ]
        for reg_name, value in targets.items():
            cmd += ["--target", f"{reg_name}={value}"]

        if self.verbose:
            print(f"[bmc] {' '.join(cmd)}")

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_ms / 1000 + 10,
            )
        except subprocess.TimeoutExpired:
            if self.verbose:
                print("[bmc] timed out")
            return None

        # BMC binary: exit 0 = sat, exit 2 = unsat/not found
        if proc.returncode == 2:
            if self.verbose:
                print(f"[bmc] no path to {targets}")
            return None

        if proc.returncode != 0:
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

        # clk2fflogic produces 2 SMT2 steps per clock cycle (clk=0 then clk=1).
        # Keep only the posedge (clk=1) steps so each entry maps 1:1 to a
        # Verilator clock cycle.  If the clock port isn't present (unexpected),
        # fall back to keeping all steps.
        clk = self.design.clock_port
        posedge_steps = [s for s in steps if s.get(clk, 1) == 1]
        if not posedge_steps:
            posedge_steps = steps  # fallback

        clean_steps = [
            {k: v for k, v in s.items() if k != clk}
            for s in posedge_steps
        ]

        return BmcResult(
            depth=data.get("depth", len(steps)),
            steps=clean_steps,
            target=targets,
        )
