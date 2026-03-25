"""
state_forcing.py — Replay a BMC input sequence into the Verilator simulation
to force the design into a specific state.
"""
from __future__ import annotations

from .bmc_interface import BmcResult
from .design_parser import DesignInfo
from .sim_driver import SimDriver


class StateForcer:
    """
    Two strategies for reaching a target state:

    A. **Stimulus Replay** (default, always correct):
       Reset the simulator to zero-init, then replay the BMC input sequence
       cycle by cycle.  Because the BMC starts from the same zero-init
       assumption, the replay is guaranteed to reach the target.

    B. **Direct Register Force** (fast path):
       Write register values directly into the Verilated model.  Works for
       any state reachable in a single write, but the forced values may be
       overwritten by the DUT's combinational/sequential logic on the next
       clock edge if the inputs are not held at a "neutral" value.
    """

    def __init__(self, driver: SimDriver, design: DesignInfo):
        self.driver = driver
        self.design = design

    # ------------------------------------------------------------------ #
    # Strategy A: stimulus replay                                          #
    # ------------------------------------------------------------------ #

    def replay_sequence(
        self, bmc_result: BmcResult, reset_first: bool = True
    ) -> list[dict[str, int]]:
        """
        Replay *bmc_result.steps* through the live simulation.
        *reset_first* should be True when the BMC sequence starts from
        ``assume_zero_init`` (the default).

        Returns a list of register state snapshots — one per replay step.
        Recording every intermediate state ensures coverage progress even when
        the SMT2 model's predicted final state diverges from the RTL simulation.
        """
        if reset_first:
            self.driver.reset(cycles=5)

        states: list[dict[str, int]] = []
        for step_inputs in bmc_result.steps:
            # Map BMC step inputs (may include "clk") to data-port dict
            filtered = {
                k: v for k, v in step_inputs.items()
                if k != self.design.clock_port
            }
            # Fill in any missing ports with 0
            for p in self.design.data_inputs:
                filtered.setdefault(p.name, 0)

            states.append(self.driver.step(filtered))

        return states

    # ------------------------------------------------------------------ #
    # Strategy B: direct force                                             #
    # ------------------------------------------------------------------ #

    def force_state(self, target_state: dict[str, int]) -> dict[str, int]:
        """
        Directly write *target_state* ({arch_name: value}) into the
        simulation registers, then step once with neutral inputs to let
        combinational logic settle.

        Returns the state snapshot after one settling cycle.
        """
        self.driver.force_state(target_state)

        # Step once with all data inputs at 0 (neutral) to let the
        # sequential logic see the forced state without changing it
        neutral = {p.name: 0 for p in self.design.data_inputs}
        return self.driver.step(neutral)
