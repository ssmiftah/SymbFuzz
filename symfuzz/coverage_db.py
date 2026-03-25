"""
coverage_db.py — SQLite-backed state coverage database.

Coverage metric: the set of observed architectural register value tuples.
Each unique combination of (reg0_value, reg1_value, ...) is one "state".
"""
from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .design_parser import DesignInfo


def _state_hash(state: dict[str, int]) -> int:
    """Stable signed 64-bit hash of a state dict (fits in SQLite INTEGER)."""
    blob = json.dumps(state, sort_keys=True).encode()
    return int.from_bytes(hashlib.sha256(blob).digest()[:8], "little", signed=True)


class CoverageDB:
    def __init__(self, db_path: str | Path, design: DesignInfo):
        self.db_path = str(db_path)
        self.design  = design
        self._conn   = sqlite3.connect(self.db_path)
        self._init_schema()
        self._last_new_cycle = 0
        self._cycle = 0
        # BMC-proven unreachable targets: frozenset of (reg, val) pairs
        self._exhausted_targets: set[frozenset] = set()

    # ------------------------------------------------------------------ #
    # Schema                                                               #
    # ------------------------------------------------------------------ #

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS visited_states (
                state_hash  INTEGER PRIMARY KEY,
                state_json  TEXT    NOT NULL,
                first_seen  REAL    NOT NULL,
                cycle_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coverage_log (
                cycle        INTEGER,
                new_states   INTEGER,
                total_states INTEGER
            );
        """)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Record / query                                                       #
    # ------------------------------------------------------------------ #

    def record_state(self, state: dict[str, int], cycle: int) -> bool:
        """
        Record *state*. Returns ``True`` if this is a new (previously unseen) state.
        """
        self._cycle = cycle
        h = _state_hash(state)
        cur = self._conn.execute(
            "SELECT 1 FROM visited_states WHERE state_hash = ?", (h,)
        )
        if cur.fetchone():
            return False

        self._conn.execute(
            "INSERT INTO visited_states VALUES (?, ?, ?, ?)",
            (h, json.dumps(state, sort_keys=True), time.monotonic(), cycle),
        )
        total = self.total_states()
        self._conn.execute(
            "INSERT INTO coverage_log VALUES (?, ?, ?)", (cycle, 1, total)
        )
        self._conn.commit()
        self._last_new_cycle = cycle
        return True

    def is_visited(self, state: dict[str, int]) -> bool:
        h = _state_hash(state)
        return bool(
            self._conn.execute(
                "SELECT 1 FROM visited_states WHERE state_hash = ?", (h,)
            ).fetchone()
        )

    def total_states(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM visited_states"
        ).fetchone()[0]

    def stale_since(self) -> int:
        """Number of cycles since the last new state was discovered."""
        return self._cycle - self._last_new_cycle

    # ------------------------------------------------------------------ #
    # Target suggestion                                                    #
    # ------------------------------------------------------------------ #

    def mark_target_exhausted(self, target: dict[str, int]) -> None:
        """Record that BMC proved *target* is unreachable; skip it in future."""
        self._exhausted_targets.add(frozenset(target.items()))

    def get_unvisited_neighbor_target(self) -> Optional[dict[str, int]]:
        """
        Suggest a single-register constraint ``{arch_name: value}`` that has NOT
        yet been seen in the coverage database.  Iterates registers in round-robin
        order across calls.  Returns ``None`` when full coverage is achieved.
        """
        all_rows = self._conn.execute(
            "SELECT state_json FROM visited_states"
        ).fetchall()
        visited_states = [json.loads(r[0]) for r in all_rows]

        registers = self.design.registers
        if not registers:
            return None

        # For each register find all observed values
        seen_values: dict[str, set[int]] = {r.arch_name: set() for r in registers}
        for s in visited_states:
            for reg in registers:
                if reg.arch_name in s:
                    seen_values[reg.arch_name].add(s[reg.arch_name])

        # Pick the register with the largest "unseen value space" first
        best_reg = None
        best_unseen = 0
        for reg in registers:
            full_space = 1 << reg.width
            unseen = full_space - len(seen_values[reg.arch_name])
            if unseen > best_unseen:
                best_unseen = unseen
                best_reg = reg

        # Single-register targeting: only when some individual values are unseen
        if best_reg is not None and best_unseen > 0:
            # Pick the smallest unseen, non-exhausted value for the best register
            seen = seen_values[best_reg.arch_name]
            for val in range(1 << best_reg.width):
                if val not in seen:
                    candidate = {best_reg.arch_name: val}
                    if frozenset(candidate.items()) not in self._exhausted_targets:
                        return candidate

            # All unseen values for best_reg are exhausted; fall back to any
            # register that still has a non-exhausted unseen value
            for reg in registers:
                seen2 = seen_values[reg.arch_name]
                for val in range(1 << reg.width):
                    if val not in seen2:
                        candidate = {reg.arch_name: val}
                        if frozenset(candidate.items()) not in self._exhausted_targets:
                            return candidate

        # ---- Fallback: joint state constraint ---------------------------
        # All individual register values have been observed, but some joint
        # states may still be unvisited.  Pick the first unvisited combination.
        visited_set: set[frozenset] = {
            frozenset(s.items()) for s in visited_states
        }
        ranges = [range(1 << r.width) for r in registers]
        for combo in itertools.product(*ranges):
            candidate = {r.arch_name: v for r, v in zip(registers, combo)}
            key = frozenset(candidate.items())
            if key not in visited_set and key not in self._exhausted_targets:
                return candidate

        return None  # all unvisited values are either covered or proven unreachable

    # ------------------------------------------------------------------ #
    # Reporting                                                            #
    # ------------------------------------------------------------------ #

    def coverage_pct(self) -> float:
        total = self.total_states()
        max_states = self.design.max_state_space
        if max_states == 0:
            return 0.0
        return min(100.0, total / max_states * 100)

    def summary(self) -> str:
        total    = self.total_states()
        max_s    = self.design.max_state_space
        pct      = self.coverage_pct()
        too_big  = max_s > (1 << 20)
        space_str = f"{max_s}" if not too_big else ">1M"
        return (
            f"States visited: {total} / {space_str}  "
            f"({pct:.1f}%)"
        )

    def close(self) -> None:
        self._conn.close()
