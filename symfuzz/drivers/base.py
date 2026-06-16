"""
drivers/base.py — Shared JSON-over-stdio driver body.

Backend-specific subclasses (XsimDriver, VerilatorDriver) override only
``compile()`` and ``load()``. Everything else — the JSON protocol, step /
reset / read_state / force / step_trace / checkpoint / restore — is shared
because both backends speak the same line-delimited JSON protocol.
"""
from __future__ import annotations

import json
import random
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..design_parser import DesignInfo


@dataclass
class CoverageSnapshot:
    """Native-coverage snapshot returned by ``collect_coverage``.

    ``points`` is the set of point IDs (strings) covered *at least once*
    since simulation start. ``universe`` is the full set of instrumented
    point IDs the backend knows about — covered plus uncovered. The
    orchestrator uses ``universe - points`` for coverage-driven BMC
    targeting. ``kinds`` maps each point ID to its category label
    ("line", "toggle", ...). ``details`` optionally carries structured
    information (e.g. ``{"reg": "...", "bit": N}``) useful for target
    synthesis. ``total`` is the count of instrumented points, used for
    the final banner percentage.
    """
    points:   set[str]               = field(default_factory=set)
    kinds:    dict[str, str]         = field(default_factory=dict)
    universe: set[str]               = field(default_factory=set)
    details:  dict[str, dict]        = field(default_factory=dict)
    total:    Optional[int]          = None


class BaseStdioDriver:
    """Common JSON-line stdio driver. Subclasses must set ``self._proc``."""

    backend_name: str = "base"

    def __init__(self, design: DesignInfo, tb_dir: str | Path, verbose: bool = False):
        self.design  = design
        self.tb_dir  = Path(tb_dir)
        self.verbose = verbose
        self._proc: Optional[subprocess.Popen] = None
        # Timing stats consumed by the orchestrator's progress logger.
        self.last_cov_duration_ms:  float = 0.0
        self.total_cov_duration_ms: float = 0.0
        self.total_cov_calls:       int   = 0

    # ---- overridden by subclasses ------------------------------------ #

    def compile(self) -> None:
        raise NotImplementedError

    def load(self) -> None:
        raise NotImplementedError

    # ---- shared lifecycle -------------------------------------------- #

    def close(self) -> None:
        if self._proc:
            try:
                line = json.dumps({"cmd": "quit"}, separators=(',', ':')) + "\n"
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except Exception:
                pass
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            self._proc = None

    # ---- internal protocol ------------------------------------------- #

    def _send(self, obj: dict) -> dict:
        assert self._proc, "Call load() first"
        self._proc.stdin.write(json.dumps(obj, separators=(',', ':')) + "\n")
        self._proc.stdin.flush()
        return self._recv()

    def _recv(self) -> dict:
        """Read stdout until a JSON line, skipping any banner/echo lines."""
        assert self._proc
        while True:
            raw = self._proc.stdout.readline()
            if not raw:
                raise RuntimeError(f"{self.backend_name} process closed unexpectedly")
            raw = raw.strip()
            if raw.startswith('{'):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    if self.verbose:
                        print(f"[sim] skip non-JSON brace line: {raw!r}")
                    continue

    # ---- simulation commands ----------------------------------------- #

    def step(self, inputs: dict[str, int]) -> dict[str, int]:
        resp = self._send({"cmd": "step", "inputs": inputs})
        if "error" in resp:
            raise RuntimeError(f"step error: {resp['error']}")
        return resp.get("state", {})

    def step_trace(
        self, inputs_list: list[dict[str, int]]
    ) -> tuple[list[dict[str, int]], "str | None"]:
        if not inputs_list:
            return [], None
        resp = self._send({"cmd": "step_trace", "inputs": inputs_list})
        states = resp.get("states", [])
        err = resp.get("error")
        return states, err

    def step_raw(self, inputs: dict[str, int]) -> dict[str, int]:
        """Apply one SMT2-style transition — set every input (clock
        included, from the caller) and advance a single ``eval`` /
        ``run``. Returns the resulting state.

        Unlike :meth:`step` (which wraps a full clk-0→1 posedge cycle),
        ``step_raw`` preserves the clk polarity the caller specified.
        Used by the BMC-replay path to reproduce clk2fflogic's
        alternating half-cycle transitions."""
        resp = self._send({"cmd": "step_raw", "inputs": inputs})
        if "error" in resp:
            raise RuntimeError(f"step_raw error: {resp['error']}")
        return resp.get("state", {})

    def step_raw_trace(
        self, inputs_list: list[dict[str, int]]
    ) -> tuple[list[dict[str, int]], "str | None"]:
        """Batched version of :meth:`step_raw`. Same return shape as
        :meth:`step_trace`."""
        if not inputs_list:
            return [], None
        resp = self._send({"cmd": "step_raw_trace", "inputs": inputs_list})
        states = resp.get("states", [])
        err = resp.get("error")
        return states, err

    def reset(self, cycles: int = 5) -> None:
        resp = self._send({"cmd": "reset", "cycles": cycles})
        if "error" in resp:
            raise RuntimeError(f"reset error: {resp['error']}")

    def read_state(self) -> dict[str, int]:
        resp = self._send({"cmd": "read_state"})
        if "error" in resp:
            raise RuntimeError(f"read_state error: {resp['error']}")
        return resp.get("state", {})

    def force(self, state: dict[str, int]) -> None:
        resp = self._send({"cmd": "force", "state": state})
        if "error" in resp:
            raise RuntimeError(f"force error: {resp['error']}")

    def checkpoint(self, path: str) -> None:
        resp = self._send({"cmd": "checkpoint", "path": path})
        if "error" in resp:
            raise RuntimeError(f"checkpoint error: {resp['error']}")

    def restore(self, path: str) -> None:
        resp = self._send({"cmd": "restore", "path": path})
        if "error" in resp:
            raise RuntimeError(f"restore error: {resp['error']}")

    def collect_coverage(self) -> Optional[CoverageSnapshot]:
        """Return a native-coverage snapshot, or ``None`` if the backend is
        not running with native coverage enabled. Subclasses override."""
        return None

    def random_step(self, deassert_reset: bool = True) -> dict[str, int]:
        inputs: dict[str, int] = {}
        for p in self.design.data_inputs:
            inputs[p.name] = random.randint(0, p.mask)
        if deassert_reset and self.design.reset_port:
            rp = self.design.reset_port
            inputs[rp] = 1 if rp.endswith(("_n", "_ni", "_b")) else 0
        return self.step(inputs)

    # ---- Runtime register-validity probe ----------------------------- #

    def validate_registers(self) -> tuple[int, list[str]]:
        """Drop registers that the simulator has folded into stub variables.

        Verilator's --public-flat-rw exposes every flop field on the
        rootp struct, so the harness compiles regardless of whether the
        flop is genuinely modeled or has been folded to a constant by
        optimization (e.g. parameter-gated dead paths from
        ``if (CFG.X)`` with X=0). Folded fields are writable from C++
        but the simulation ignores them — force_state writes a residual
        variable nothing reads from, and reset cycles don't drive it.
        BMC SAT witnesses against such registers replay without coverage
        motion.

        Detection — reset-response: force all registers to two
        complementary non-zero patterns, run ``reset()`` after each
        force, and read back. Real flops have their always_ff reset
        clauses connected to rst_n, so reset overrides the forced
        value (typically driving it to 0 or to the design's reset
        constant). A folded stub doesn't respond to reset because the
        simulator has no driver wired to the variable — it retains the
        forced bits verbatim. Require BOTH patterns to leak through
        unchanged to flag the register as dead, ruling out designs
        where the reset constant happens to equal one pattern.

        Reset is preferred over random stepping because hold-on-no-op
        flops (e.g. CSR registers whose write enable rarely fires) look
        indistinguishable from folded stubs under random stimulation —
        both retain the forced value. Reset, by contrast, is mandatory
        sensitivity for any modeled flop.

        Caller contract: invoke once immediately after ``load()``.
        Leaves the design in reset.
        """
        regs = list(self.design.registers)
        if not regs:
            return 0, []
        # Without a reset port the leak-detection criterion (reset
        # overrides forced bits) is meaningless: reset() becomes a
        # no-op so EVERY register's value would "leak through" and we'd
        # drop the entire list. Skip the probe in that case.
        if not self.design.reset_port:
            return 0, []

        ALT_LO = 0x5555_5555_5555_5555
        ALT_HI = 0xAAAA_AAAA_AAAA_AAAA
        patterns: list[dict[str, int]] = [
            {r.arch_name: r.mask                for r in regs},  # all-ones
            {r.arch_name: r.mask & ALT_LO       for r in regs},  # 0101...
            {r.arch_name: r.mask & ALT_HI       for r in regs},  # 1010...
        ]

        # Per-register: count how many patterns survived reset intact.
        leak_count = {r.arch_name: 0 for r in regs}
        for pat in patterns:
            # Force *before* reset: the force write happens between
            # cycles, reset() then drives the design through several
            # rst-asserted clock edges. A real flop's reset clause
            # latches the design's reset constant; a folded stub holds
            # whatever bits we wrote.
            self.force(pat)
            self.reset()
            state = self.read_state()
            for r in regs:
                v = state.get(r.arch_name)
                if v is None:
                    leak_count[r.arch_name] += 1
                    continue
                if v == pat[r.arch_name]:
                    leak_count[r.arch_name] += 1

        alive: list = []
        dead: list[str] = []
        threshold = len(patterns)  # require ALL patterns to leak
        for r in regs:
            if leak_count[r.arch_name] >= threshold:
                dead.append(r.arch_name)
            else:
                alive.append(r)

        self.design.registers = alive
        self.reset()
        return len(dead), dead
