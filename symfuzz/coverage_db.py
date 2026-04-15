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


# Public alias so other modules (e.g. corpus_store) can compute the same key
state_hash = _state_hash


def _target_key(target: dict) -> tuple:
    """
    Hashable representation of a BMC target dict. Supports legacy
    ``{reg: int}`` and bit-level ``{reg: {"bit": K, "val": V}}`` shapes.
    frozenset(items()) doesn't work when values are dicts (unhashable).
    """
    return tuple(sorted((k, json.dumps(v, sort_keys=True))
                        for k, v in target.items()))


class CoverageDB:
    def __init__(self, db_path: str | Path, design: DesignInfo):
        self.db_path = str(db_path)
        self.design  = design
        self._conn   = sqlite3.connect(self.db_path)
        self._init_schema()
        self._last_new_cycle = 0
        self._last_new_native_cycle = 0
        self._cycle = 0
        # BMC-proven unreachable targets: frozenset of (reg, val) pairs
        self._exhausted_targets: set[frozenset] = set()
        # Coverage point IDs retired by the unproductive-BMC check.
        self._exhausted_coverage_points: set[str] = set()
        # Per-point BMC tier (0 = cheapest). Promoted on timeout/UNSAT at
        # non-top tier; retired via _exhausted_coverage_points at top tier.
        self._point_tier: dict[str, int] = {}

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
            CREATE TABLE IF NOT EXISTS coverage_points (
                point_id         TEXT    PRIMARY KEY,
                backend          TEXT    NOT NULL,
                kind             TEXT    NOT NULL,
                first_seen_cycle INTEGER NOT NULL,
                first_seen_time  REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coverage_point_universe (
                point_id     TEXT PRIMARY KEY,
                kind         TEXT NOT NULL,
                details_json TEXT
            );
            CREATE TABLE IF NOT EXISTS value_class_hits (
                point_id  TEXT NOT NULL,
                signature BLOB NOT NULL,
                first_seen_cycle INTEGER NOT NULL,
                PRIMARY KEY (point_id, signature)
            );
            CREATE INDEX IF NOT EXISTS idx_vc_hits_pid
                ON value_class_hits(point_id);
            CREATE TABLE IF NOT EXISTS corpus_entries (
                state_hash         INTEGER PRIMARY KEY,
                segments_json      TEXT    NOT NULL,
                total_depth        INTEGER NOT NULL,
                source             TEXT    NOT NULL,
                wdb_path           TEXT,
                hit_count          INTEGER NOT NULL DEFAULT 0,
                created            REAL    NOT NULL,
                contribution_score INTEGER NOT NULL DEFAULT 1
            );
        """)
        # Idempotent migration for pre-existing DBs written before the
        # contribution_score column existed.
        try:
            self._conn.execute(
                "ALTER TABLE corpus_entries "
                "ADD COLUMN contribution_score INTEGER NOT NULL DEFAULT 1"
            )
        except sqlite3.OperationalError:
            pass  # column already present — nothing to do
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Record / query                                                       #
    # ------------------------------------------------------------------ #

    def record_state(self, state: dict[str, int], cycle: int) -> bool:
        """
        Record *state*. Returns ``True`` if this is a new (previously unseen) state.

        ``self._cycle`` is monotonic: callers may pass a ``cycle`` that's
        been reset by BMC replay (the orchestrator's ``global_cycle``
        resets to 0 after each replay), so we keep the larger value so
        ``stale_since`` measures true wall-cycles, not replay-relative.
        """
        self._cycle = max(self._cycle, cycle)
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

    def stale_since(self, source: str = "tuple") -> int:
        """
        Cycles since the last *new coverage event*. *source* selects which
        signal to consult:

        - ``"tuple"`` *(default)*: new register-tuple states (classic).
        - ``"native"``: new native-coverage points (requires ``--native-coverage``).
        - ``"any"``: the more recent of the two (shortest idle).

        Default preserves byte-identical behaviour for existing callers.
        """
        if source == "tuple":
            return self._cycle - self._last_new_cycle
        if source == "native":
            return self._cycle - self._last_new_native_cycle
        if source == "any":
            return self._cycle - max(self._last_new_cycle,
                                     self._last_new_native_cycle)
        raise ValueError(f"unknown stall source: {source!r}")

    # ------------------------------------------------------------------ #
    # Target suggestion                                                    #
    # ------------------------------------------------------------------ #

    def mark_target_exhausted(self, target: dict[str, int]) -> None:
        """Record that BMC proved *target* is unreachable; skip it in future."""
        self._exhausted_targets.add(_target_key(target))

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
                    if _target_key(candidate) not in self._exhausted_targets:
                        return candidate

            # All unseen values for best_reg are exhausted; fall back to any
            # register that still has a non-exhausted unseen value
            for reg in registers:
                seen2 = seen_values[reg.arch_name]
                for val in range(1 << reg.width):
                    if val not in seen2:
                        candidate = {reg.arch_name: val}
                        if _target_key(candidate) not in self._exhausted_targets:
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
            visited_key   = frozenset(candidate.items())
            exhausted_key = _target_key(candidate)
            if visited_key not in visited_set and exhausted_key not in self._exhausted_targets:
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

    # ------------------------------------------------------------------ #
    # Native coverage                                                      #
    # ------------------------------------------------------------------ #

    def record_coverage_snapshot(self, snapshot, cycle: int,
                                 backend: str) -> int:
        """
        Upsert every point in *snapshot* into ``coverage_points`` and the
        full instrumented *universe* into ``coverage_point_universe``.
        Returns the count of newly-inserted covered points. The list of
        new point IDs is stashed on ``self.last_new_point_ids`` for any
        caller that needs attribution (value-class coverage).
        """
        self.last_new_point_ids: list[str] = []
        if snapshot is None:
            return 0
        now = time.monotonic()
        new = 0
        for pid in snapshot.points:
            cur = self._conn.execute(
                "SELECT 1 FROM coverage_points WHERE point_id = ?", (pid,)
            )
            if cur.fetchone():
                continue
            kind = snapshot.kinds.get(pid, "unknown")
            self._conn.execute(
                "INSERT INTO coverage_points VALUES (?, ?, ?, ?, ?)",
                (pid, backend, kind, cycle, now),
            )
            new += 1
            self.last_new_point_ids.append(pid)
        # Universe: upsert regardless of covered/uncovered so
        # get_uncovered_coverage_target can subtract the covered set.
        for pid in snapshot.universe:
            details = snapshot.details.get(pid)
            self._conn.execute(
                "INSERT OR IGNORE INTO coverage_point_universe VALUES (?, ?, ?)",
                (pid, snapshot.kinds.get(pid, "unknown"),
                 json.dumps(details) if details else None),
            )
        self._cycle = max(self._cycle, cycle)
        if new:
            self._last_new_cycle = cycle
            self._last_new_native_cycle = cycle
        self._conn.commit()
        self._latest_native_total = snapshot.total
        return new

    def record_value_class_hit(self, point_id: str, signature: bytes,
                               cycle: int) -> bool:
        """Record that *point_id* was hit under *signature*. Returns True
        when the (point, signature) pair is newly recorded."""
        if not point_id or not signature:
            return False
        try:
            self._conn.execute(
                "INSERT INTO value_class_hits VALUES (?, ?, ?)",
                (point_id, signature, cycle),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def value_class_diversification(self, target_n: int) -> tuple[int, int]:
        """Return (diversified_points, total_points). A point is diversified
        when it has been observed under >= *target_n* distinct signatures."""
        total = self._conn.execute(
            "SELECT COUNT(*) FROM coverage_point_universe"
        ).fetchone()[0]
        if target_n <= 0:
            return (total, total)
        diversified = self._conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT point_id FROM value_class_hits "
            "  GROUP BY point_id HAVING COUNT(*) >= ?"
            ")", (target_n,)
        ).fetchone()[0]
        return (diversified, total)

    def get_under_diversified_point(
        self, target_n: int,
    ) -> Optional[tuple[str, list[bytes]]]:
        """Return (point_id, seen_signatures) for a covered bin whose distinct-
        signature count is below *target_n*, or None if every covered bin is
        diversified. Prefers bins with the fewest signatures (closest to
        target) so seed-biasing effort goes where it moves the needle."""
        row = self._conn.execute(
            "SELECT point_id, COUNT(*) AS n "
            "FROM value_class_hits "
            "WHERE point_id IN (SELECT point_id FROM coverage_points) "
            "GROUP BY point_id "
            "HAVING n < ? "
            "ORDER BY n DESC "
            "LIMIT 1",
            (target_n,)
        ).fetchone()
        if not row:
            return None
        pid = row[0]
        sigs = [r[0] for r in self._conn.execute(
            "SELECT signature FROM value_class_hits WHERE point_id = ?",
            (pid,)
        ).fetchall()]
        return (pid, sigs)

    def value_class_per_point_counts(self) -> list[tuple[str, int]]:
        """Return list of (point_id, distinct_signature_count) for all
        points that have at least one recorded signature."""
        rows = self._conn.execute(
            "SELECT point_id, COUNT(*) FROM value_class_hits "
            "GROUP BY point_id ORDER BY COUNT(*) ASC"
        ).fetchall()
        return [(pid, int(n)) for pid, n in rows]

    def promote_coverage_point(self, point_id: str, max_tier: int) -> bool:
        """Bump *point_id* one BMC-budget tier. Returns True if the point is
        still within *max_tier* (i.e. promotion succeeded); False if the next
        tier would exceed max — caller should blacklist instead."""
        if not point_id:
            return False
        cur = self._point_tier.get(point_id, 0)
        nxt = cur + 1
        if nxt > max_tier:
            return False
        self._point_tier[point_id] = nxt
        return True

    def get_point_tier(self, point_id: str) -> int:
        return self._point_tier.get(point_id, 0) if point_id else 0

    def mark_coverage_point_exhausted(self, point_id: str) -> None:
        """Retire a native coverage point from future BMC target selection.
        Used by the unproductive-BMC retirement path: if BMC kept returning
        SAT on a synthesized target from this point but the replay added no
        coverage, we stop revisiting that point."""
        if point_id:
            self._exhausted_coverage_points.add(point_id)

    def get_uncovered_coverage_target(
        self, rarity_weighted: bool = False,
    ) -> Optional[dict]:
        """
        Return a structured target dict for the next uncovered native-coverage
        point (toggles first), or None if the universe is empty or every
        uncovered point has been retired. The dict is consumed by the
        orchestrator's toggle → BMC register-value translator.

        When *rarity_weighted* is True, prefer bins whose sibling bits on the
        same register are already covered (near-reachable) over bins in regs
        with zero covered bits (likely dead or unreachable under the current
        harness).
        """
        rows = self._conn.execute(
            "SELECT point_id, kind, details_json "
            "FROM coverage_point_universe "
            "WHERE point_id NOT IN (SELECT point_id FROM coverage_points) "
            "ORDER BY CASE WHEN kind='toggle' THEN 0 ELSE 1 END, RANDOM() "
            "LIMIT 256"
        ).fetchall()
        # Rarity scoring: per-reg covered-bit count, used to rank candidates.
        reg_hits: dict[str, int] = {}
        if rarity_weighted:
            cov_rows = self._conn.execute(
                "SELECT point_id FROM coverage_points"
            ).fetchall()
            for (pid,) in cov_rows:
                if pid.startswith("xtgl:"):
                    parts = pid.split(":")
                    if len(parts) >= 4:
                        reg_hits[parts[2]] = reg_hits.get(parts[2], 0) + 1
        # Prefer lowest-tier (cheapest BMC budget) candidates first; only
        # escalate to promoted points once the tier-0 pool is drained.
        candidates = []
        for point_id, kind, details_json in rows:
            if point_id in self._exhausted_coverage_points:
                continue
            tier = self._point_tier.get(point_id, 0)
            # Rarity key: -siblings_covered (more siblings → higher priority).
            rarity_key = 0
            if rarity_weighted and point_id.startswith("xtgl:"):
                parts = point_id.split(":")
                if len(parts) >= 4:
                    rarity_key = -reg_hits.get(parts[2], 0)
            candidates.append((tier, rarity_key, point_id, kind, details_json))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x[0], x[1]))
        tier, _rk, point_id, kind, details_json = candidates[0]
        details = json.loads(details_json) if details_json else {}
        details["point_id"] = point_id
        details["kind"]     = kind
        details["tier"]     = tier
        return details

    def native_covered(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM coverage_points"
        ).fetchone()[0]

    def is_native_covered(self, point_id: str) -> bool:
        """True iff *point_id* is in ``coverage_points`` (i.e. the simulator
        has reported it hit at least once). Used by the orchestrator's strict
        productivity check to decide whether a coverage_toggle BMC call
        actually exercised the bit it targeted."""
        if not point_id:
            return False
        return bool(self._conn.execute(
            "SELECT 1 FROM coverage_points WHERE point_id = ?",
            (point_id,)
        ).fetchone())

    def is_native_complete(self) -> bool:
        """True when the simulator-native coverage universe is fully hit.

        Consulted by the orchestrator to short-circuit termination when
        every instrumented point the backend knows about has been covered.
        Returns False when native coverage is disabled / not yet populated.
        """
        total = getattr(self, "_latest_native_total", None)
        if not total:
            return False
        return self.native_covered() >= total

    def native_covered_by_kind(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT kind, COUNT(*) FROM coverage_points GROUP BY kind"
        ).fetchall()
        return {k: n for k, n in rows}

    def native_coverage_pct(self) -> Optional[float]:
        total = getattr(self, "_latest_native_total", None)
        if not total:
            return None
        return 100.0 * self.native_covered() / total

    def native_summary(self) -> str:
        by_kind = self.native_covered_by_kind()
        parts = [f"{n} {k}" for k, n in sorted(by_kind.items())]
        pct = self.native_coverage_pct()
        covered = self.native_covered()
        total = getattr(self, "_latest_native_total", None)
        denom = f"/{total}" if total else ""
        body  = ", ".join(parts) if parts else "0"
        suffix = f" ({pct:.1f}%)" if pct is not None else ""
        return f"{covered}{denom} points — {body}{suffix}"

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        self._conn.close()
