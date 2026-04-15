"""
mutations.py — AFL-style mutation strategies for SymbFuzz.

Each strategy is a generator of one burst's worth of input dicts. The caller
(the Orchestrator) iterates the generator, calls driver.step(inputs), records
coverage, and bails out early if new coverage is discovered (the mutation's
"crash" equivalent).

Phase 1 strategies:
  - havoc : roll fresh random inputs for up to *burst* cycles.
  - splice: apply the tail of another corpus entry's segments.

Both strategies assume the simulator has already been restored to the seed
entry's state. Strategies do not handle reset — the orchestrator does that.
"""
from __future__ import annotations

import random
from typing import Iterator

from .corpus_store import CorpusEntry, _expand_segments
from .design_parser import DesignInfo


def _random_inputs(design: DesignInfo, rng: random.Random) -> dict[str, int]:
    inputs: dict[str, int] = {}
    for p in design.data_inputs:
        inputs[p.name] = rng.randint(0, p.mask)
    if design.reset_port:
        rp = design.reset_port
        # Keep reset de-asserted during mutations so we don't accidentally wipe
        # the state we just restored to.
        inputs[rp] = 1 if rp.endswith(("_n", "_ni", "_b")) else 0
    return inputs


def havoc(
    design: DesignInfo, burst: int, rng: random.Random
) -> Iterator[dict[str, int]]:
    """Yield up to *burst* fresh random input dicts."""
    n = rng.randint(1, max(1, burst))
    for _ in range(n):
        yield _random_inputs(design, rng)


def splice(
    design:      DesignInfo,
    other:       CorpusEntry,
    burst:       int,
    rng:         random.Random,
) -> Iterator[dict[str, int]]:
    """
    Apply the tail of *other*'s input sequence — from a random offset — for up
    to *burst* cycles. The seed entry's state is the starting point (caller
    has already restored to it). Missing data inputs are zero-filled.
    """
    if not other.segments:
        return
    total = other.total_depth
    if total <= 1:
        return
    offset = rng.randint(0, total - 1)
    remaining = min(burst, total - offset)
    seen = 0
    skipped = 0
    for inputs in _expand_segments(other.segments):
        if skipped < offset:
            skipped += 1
            continue
        if seen >= remaining:
            break
        full = {p.name: 0 for p in design.data_inputs}
        full.update(inputs)
        if design.reset_port:
            rp = design.reset_port
            full[rp] = 1 if rp.endswith(("_n", "_ni", "_b")) else 0
        yield full
        seen += 1
