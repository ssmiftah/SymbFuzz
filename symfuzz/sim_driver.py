"""
sim_driver.py — Drive the Vivado xsim simulation via a TCL bridge subprocess.

Protocol: line-delimited compact JSON over stdin/stdout.
  → {"cmd":"step","inputs":{name:val,...}}   ← {"state":{arch_name:val,...}}
  → {"cmd":"reset","cycles":N}               ← {"ok":true}
  → {"cmd":"read_state"}                     ← {"state":{arch_name:val,...}}
  → {"cmd":"force","state":{arch_name:val,...}} ← {"ok":true}
  → {"cmd":"quit"}

xsim prints its banner and TCL echo lines to stdout; _recv() skips all lines
that do not start with '{'.
"""
from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path
from typing import Optional

from .design_parser import DesignInfo


class SimDriver:
    def __init__(self, design: DesignInfo, tb_dir: str | Path, verbose: bool = False):
        self.design  = design
        self.tb_dir  = Path(tb_dir)
        self.verbose = verbose
        self._proc: Optional[subprocess.Popen] = None

    # ---- lifecycle --------------------------------------------------- #

    def compile(self) -> None:
        """Run compile_<module>.sh (xvlog + xelab)."""
        script = self.tb_dir / f"compile_{self.design.module_name}.sh"
        if self.verbose:
            print(f"[symfuzz] Compiling with Vivado xsim ...")
        result = subprocess.run(
            ["bash", str(script)],
            cwd=str(self.tb_dir),
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"xsim compile failed:\n{result.stdout}\n{result.stderr}")
        if self.verbose:
            print(result.stdout.strip())

    def load(self) -> None:
        """Spawn xsim subprocess with the TCL bridge."""
        snapshot   = f"{self.design.module_name}_sim"
        bridge_tcl = str(self.tb_dir / f"tcl_bridge_{self.design.module_name}.tcl")
        if self.verbose:
            print(f"[symfuzz] Starting xsim {snapshot} ...")
        self._proc = subprocess.Popen(
            ["xsim", snapshot, "--tclbatch", bridge_tcl],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
            cwd=str(self.tb_dir),
        )

    def close(self) -> None:
        if self._proc:
            # Send quit without waiting for a response (xsim emits cleanup
            # lines after the TCL loop exits, so _recv() would block forever).
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
        """Read stdout until a JSON line, skipping xsim banner/echo/INFO lines."""
        assert self._proc
        while True:
            raw = self._proc.stdout.readline()
            if not raw:
                raise RuntimeError("xsim process closed unexpectedly")
            raw = raw.strip()
            if raw.startswith('{'):
                return json.loads(raw)

    # ---- simulation commands ----------------------------------------- #

    def step(self, inputs: dict[str, int]) -> dict[str, int]:
        resp = self._send({"cmd": "step", "inputs": inputs})
        if "error" in resp:
            raise RuntimeError(f"step error: {resp['error']}")
        return resp.get("state", {})

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

    def random_step(self, deassert_reset: bool = True) -> dict[str, int]:
        inputs: dict[str, int] = {}
        for p in self.design.data_inputs:
            inputs[p.name] = random.randint(0, p.mask)
        if deassert_reset and self.design.reset_port:
            rp = self.design.reset_port
            inputs[rp] = 1 if rp.endswith(("_n", "_ni", "_b")) else 0
        return self.step(inputs)
