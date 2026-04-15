"""
drivers — simulator-backend selection.

Both backends speak the same JSON-over-stdio protocol. Orchestrator /
corpus_store / state_forcing / bmc code is backend-agnostic.
"""
from __future__ import annotations

from pathlib import Path

from ..design_parser import DesignInfo


def make_driver(
    backend: str,
    design:  DesignInfo,
    tb_dir:  str | Path,
    verbose: bool = False,
    grey_bit_samples: int = 4,
):
    if backend == "xsim":
        from .xsim import XsimDriver
        return XsimDriver(design, tb_dir, verbose,
                          grey_bit_samples=grey_bit_samples)
    if backend == "verilator":
        from .verilator import VerilatorDriver
        return VerilatorDriver(design, tb_dir, verbose)
    raise ValueError(f"unknown --sim backend: {backend!r} "
                     f"(expected 'xsim' or 'verilator')")


__all__ = ["make_driver"]
