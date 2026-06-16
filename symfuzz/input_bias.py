"""
input_bias.py — Coverage-driven adaptive input biasing.

When SymbFuzz draws random values for each data input port every cycle,
uniform sampling over the full 2**N range almost never lands on the small
set of "interesting" values for control ports (CSR addresses, op codes,
state-machine commands, ...). The fuzzer wastes most of its budget on
inputs that produce no coverage motion.

This module tracks which exact values of each input port have historically
correlated with coverage growth, then biases future random draws toward
them. Pure mechanism — no RTL parsing, no spec knowledge, no per-design
configuration. The model learns whatever the design uses.

Design
------
- **Per-port whole-value frequency table.** For each input port P, keep a
  ``dict[value -> credit]``. Sampling is weighted by credit. Per-bit bias
  was considered and rejected: it loses the combinatorial signal
  ("csr_addr=0x302 AND csr_op=WRITE matters together") that the whole-value
  table preserves naturally.

- **Credit at coverage-poll boundaries.** When the orchestrator polls
  native coverage and observes N newly-covered bins, distribute N units
  of credit evenly across every input draw recorded since the last poll.
  No pipeline-depth assumption — credit slightly leaks across cycles, but
  the model is robust to that noise.

- **Epsilon-greedy sampling.** Configurable ``explore_rate`` (default
  0.3): that fraction of draws is pure uniform random; the rest is
  weighted-by-credit. Prevents lock-in to early-lucky values and keeps a
  steady stream of fresh values flowing through the design.

- **Slow exponential decay.** Every ``decay_every`` polls (default 100),
  multiply all credits by ``decay_factor`` (default 0.95). Lets the model
  refocus once the values it has been exploiting saturate their own
  reachable coverage.

The model is opt-in via YAML (``adaptive_input_bias: true``). When off,
:meth:`sample` falls through to uniform random.
"""
from __future__ import annotations

import random
from collections import deque
from typing import Iterable


class InputBiasModel:
    """Per-port frequency-based bias model with epsilon-greedy sampling.

    Lifecycle: caller invokes :meth:`sample` for each port every random
    draw; :meth:`record_draw` after each batch of draws (so the window
    knows what to credit); :meth:`credit` when the orchestrator's
    coverage poll reports a non-zero delta of newly-covered bins.
    """

    def __init__(self,
                 port_widths: dict[str, int],
                 rng: random.Random,
                 explore_rate: float = 0.3,
                 decay_factor: float = 0.95,
                 decay_every: int = 100,
                 window_cap: int = 2048,
                 recency_alpha: float = 0.95):
        self.port_masks  = {name: (1 << w) - 1 for name, w in port_widths.items()}
        self.rng         = rng
        # Width-aware per-port exploration rate. Narrow control ports
        # (≤4 bits like irq_i/ipi_i) deserve aggressive exploitation —
        # there are only a handful of values that matter and we want to
        # land on them often. Wide data ports (>24 bits like csr_wdata_i)
        # should stay near-uniform random because the bias model can't
        # learn meaningful structure across 2**32+ values — the credit
        # is dust on individual values and biasing toward them locks the
        # design into stale data without uncovering anything new. Middle
        # ports (csr_op_i 8-bit, csr_addr_i 12-bit) sit between.
        #
        # The user-passed `explore_rate` is the BASE; per-port it gets
        # scaled by a width factor. The piecewise curve and clamps below
        # produce a sensible gradient without requiring any per-design
        # tuning.
        base = float(explore_rate)
        def _explore_for_width(w: int) -> float:
            if w <= 4:    return max(0.05, base * 0.4)
            if w <= 12:   return base * 0.8
            if w <= 24:   return min(0.9, base * 1.5)
            return max(base, 0.85)        # wide: near-uniform random
        self.explore_per_port: dict[str, float] = {
            name: _explore_for_width(w) for name, w in port_widths.items()
        }
        self.explore_rate = base   # retained for diagnostics only
        self.decay_factor = float(decay_factor)
        self.decay_every  = int(decay_every)
        # Recency weighting for credit attribution. When a coverage poll
        # reports N new bins, credit is distributed across the window with
        # weight proportional to alpha**(K-1-i) where i is the draw's
        # position from oldest (0) to newest (K-1). alpha=0.95 gives a
        # half-life of ~14 draws — i.e. the last ~14 draws receive ~50%
        # of the credit, the last ~30 receive ~80%. Captures the fact
        # that the input most likely responsible for a newly-covered bin
        # is one of the most recent ones, not one from 200 cycles back.
        # alpha >= 1.0 disables recency weighting (credit spread uniformly,
        # the original behavior).
        self.recency_alpha = float(recency_alpha)
        # Per-port frequency tables: value -> accumulated credit
        self.tables: dict[str, dict[int, float]] = {n: {} for n in port_widths}
        # Per-port total credit, kept in sync for fast "is table cold?" check
        self.totals: dict[str, float] = {n: 0.0 for n in port_widths}
        # Sliding window of recent draws awaiting credit. Each entry is
        # a dict {port: value}. Capped to bound memory if credit never
        # arrives (e.g. long coverage drought).
        self.window: deque[dict[str, int]] = deque(maxlen=window_cap)
        self.poll_count = 0

    # ---- Hot path -------------------------------------------------- #

    def sample(self, port: str) -> int:
        """Draw a value for `port`. Returns uniform random when (a) the
        port's table is empty (cold start) or (b) the epsilon-greedy
        exploration coin lands; otherwise samples weighted by credit."""
        mask = self.port_masks.get(port)
        if not mask:
            return 0
        total = self.totals.get(port, 0.0)
        explore = self.explore_per_port.get(port, self.explore_rate)
        if total <= 0.0 or self.rng.random() < explore:
            return self.rng.randint(0, mask)
        table = self.tables[port]
        # random.choices is O(n) per draw; for narrow control ports the
        # table stays small (~tens of distinct values). If wide ports
        # produce thousands of distinct credited values, this is still
        # fine because we sample many fewer cycles than draws.
        values  = list(table.keys())
        weights = list(table.values())
        return self.rng.choices(values, weights=weights, k=1)[0]

    def record_draw(self, inputs: dict[str, int]) -> None:
        """Stash this draw in the credit window. Caller passes the full
        per-cycle input dict; the model only tracks ports it was
        configured with."""
        # Copy out the relevant keys so later mutation by the caller
        # can't corrupt the window. Skip unknown ports cheaply.
        kept = {p: inputs[p] for p in self.tables if p in inputs}
        if kept:
            self.window.append(kept)

    def credit(self, new_bins: int) -> None:
        """The orchestrator just polled coverage and observed `new_bins`
        newly-covered points. Distribute credit across every draw in the
        window with recency weighting (recent draws get most of the
        credit), then reset the window. Apply decay on schedule."""
        if new_bins <= 0 or not self.window:
            # Even with zero new bins, advance the poll counter so
            # decay scheduling stays honest. Drain the window since
            # those draws didn't produce coverage; we don't credit them
            # but we shouldn't bank them against a *future* burst either.
            self.window.clear()
            self.poll_count += 1
            self._maybe_decay()
            return
        K = len(self.window)
        alpha = self.recency_alpha
        if alpha >= 1.0 or alpha <= 0.0:
            # Uniform credit (recency weighting disabled).
            weights = [1.0 / K] * K
        else:
            # weight[i] proportional to alpha**(K-1-i): oldest=alpha**(K-1),
            # newest=1. Normalize so total credit equals new_bins.
            raw = [alpha ** (K - 1 - i) for i in range(K)]
            s = sum(raw)
            weights = [r / s for r in raw]
        for i, entry in enumerate(self.window):
            credit_i = new_bins * weights[i]
            if credit_i < 1e-9:
                # Negligible; skip table updates to avoid dust entries
                # for the oldest draws in a long window.
                continue
            for port, val in entry.items():
                t = self.tables[port]
                t[val] = t.get(val, 0.0) + credit_i
                self.totals[port] += credit_i
        self.window.clear()
        self.poll_count += 1
        self._maybe_decay()

    # ---- Maintenance ---------------------------------------------- #

    def _maybe_decay(self) -> None:
        if self.decay_every <= 0:
            return
        if self.poll_count % self.decay_every != 0:
            return
        for port, table in self.tables.items():
            for v in list(table.keys()):
                table[v] *= self.decay_factor
            self.totals[port] *= self.decay_factor

    # ---- Diagnostics ---------------------------------------------- #

    def summary(self, top_k: int = 3) -> dict[str, dict]:
        """Compact picture of what the model has learned. Useful for
        end-of-run reporting; cheap enough to call ad-hoc."""
        out: dict[str, dict] = {}
        for port, table in self.tables.items():
            explore = round(self.explore_per_port.get(port, self.explore_rate), 3)
            if not table:
                out[port] = {"distinct": 0, "explore": explore, "top": []}
                continue
            top = sorted(table.items(), key=lambda kv: -kv[1])[:top_k]
            out[port] = {
                "distinct": len(table),
                "explore": explore,
                "top": [(hex(v) if v >= 16 else v, round(c, 1)) for v, c in top],
            }
        return out
