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
        # Signal-level dead set, populated by mark_signal_dead. Shared
        # between the coverage-target picker and the register-target
        # picker so both layers respect the dead-signal filter.
        self._dead_signals: set[str] = set()

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

        Filters out registers whose signal name is in ``_dead_signals``
        (populated by the dead-signal filter via :meth:`mark_signal_dead`).
        Without this, the picker would keep selecting dead-extension
        registers (e.g. accelerator regs when the coproc is disabled),
        burning BMC budget on guaranteed timeouts.
        """
        all_rows = self._conn.execute(
            "SELECT state_json FROM visited_states"
        ).fetchall()
        visited_states = [json.loads(r[0]) for r in all_rows]

        registers = [r for r in self.design.registers
                     if r.arch_name not in self._dead_signals]
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

    def value_class_under_target_points(
        self, target_n: int,
    ) -> list[tuple[str, int]]:
        """Return (point_id, current_sig_count) for every covered point
        whose signature count is strictly below *target_n*. Caller uses this
        to decide which bins still need fresh signatures each poll."""
        rows = self._conn.execute(
            "SELECT cp.point_id, COUNT(v.signature) AS n "
            "FROM coverage_points cp "
            "LEFT JOIN value_class_hits v ON v.point_id = cp.point_id "
            "GROUP BY cp.point_id "
            "HAVING n < ?",
            (target_n,)
        ).fetchall()
        return [(pid, int(n)) for pid, n in rows]

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
        tier would exceed max — caller should blacklist instead.

        Suppresses promotion when the point's parent signal is in the
        "narrow-effective" set (see :meth:`_narrow_effective_signals`).
        Uncovered bins on such signals are structurally dead RTL padding
        — the register was declared 64-bit but only a handful of bits
        are functional (icache_q, mie_q, medeleg_q high bits, etc.).
        Promoting past tier-0 costs 15-60s per bin × hundreds of dead
        bins per signal; suppressing promotion saves hours of wall-clock
        while still giving each bin one tier-0 attempt.
        """
        if not point_id:
            return False
        cur = self._point_tier.get(point_id, 0)
        nxt = cur + 1
        if nxt > max_tier:
            return False
        # Narrow-effective suppression: after global stall, cap dead-heavy
        # signals at tier-0. Only fires once _last_new_native_cycle is far
        # enough back that we're confident random+BMC has had its chance.
        if cur >= 0 and self._is_point_on_narrow_effective_signal(point_id):
            return False
        self._point_tier[point_id] = nxt
        return True

    def _is_point_on_narrow_effective_signal(self, point_id: str) -> bool:
        """True when point_id's parent signal has ≤30% of its bins
        covered AND global coverage has stalled — a strong heuristic
        that the remaining uncovered bins are dead RTL padding.

        Cached in ``_narrow_effective_cache`` and invalidated whenever
        new native coverage arrives; the SQL is cheap but a
        per-BMC-call query would add up over thousands of calls."""
        # Only meaningful after stall
        if self._cycle - self._last_new_native_cycle < 500:
            return False
        # Extract signal from point_id (format: xtgl:HIER:SIG:BIT:DIR)
        parts = point_id.split(":")
        if len(parts) < 4 or parts[0] != "xtgl":
            return False
        sig = parts[2]
        # Cache per-signal narrow-effective status
        cache = getattr(self, "_narrow_effective_cache", None)
        cache_stamp = getattr(self, "_narrow_effective_stamp", None)
        if cache is None or cache_stamp != self._last_new_native_cycle:
            self._narrow_effective_cache = {}
            self._narrow_effective_stamp = self._last_new_native_cycle
            cache = self._narrow_effective_cache
        if sig in cache:
            return cache[sig]
        # Compute coverage fraction for this signal
        row = self._conn.execute(
            "SELECT SUM(CASE WHEN cp.point_id IS NULL THEN 0 ELSE 1 END) AS cov, "
            "       COUNT(*) AS tot "
            "FROM coverage_point_universe cpu "
            "LEFT JOIN coverage_points cp ON cp.point_id = cpu.point_id "
            "WHERE json_extract(cpu.details_json, '$.reg') LIKE ?",
            (f"%.{sig}",)   # signal may be a suffix (hier stripped)
        ).fetchone()
        if not row or not row[1]:
            cache[sig] = False
            return False
        cov, tot = row
        frac = cov / tot if tot else 0
        # 5% < frac ≤ 30% → narrow-effective (signal has some live bits
        # but the rest are almost certainly dead). Below 5% is handled
        # by the whole-signal dead-signal filter.
        is_narrow = 0.05 < frac <= 0.30
        cache[sig] = is_narrow
        return is_narrow

    def get_point_tier(self, point_id: str) -> int:
        return self._point_tier.get(point_id, 0) if point_id else 0

    def mark_coverage_point_exhausted(self, point_id: str) -> None:
        """Retire a native coverage point from future BMC target selection.
        Used by the unproductive-BMC retirement path: if BMC kept returning
        SAT on a synthesized target from this point but the replay added no
        coverage, we stop revisiting that point."""
        if point_id:
            self._exhausted_coverage_points.add(point_id)

    def detect_dead_signals(self,
                             stall_cycles: int = 500,
                             max_covered_fraction: float = 0.05) -> set[str]:
        """Return signal names where the fraction of covered bins is
        below ``max_covered_fraction`` AND the design has shown no new
        native coverage for at least ``stall_cycles`` cycles.

        Rationale: a signal whose toggle bins remain mostly uncovered
        even after coverage growth has plateaued (so BMC has exhausted
        its ideas for finding new bins on what IS reachable) is very
        likely *structurally unreachable* in the current design
        configuration — bins under a disabled extension, upper bits of
        a sparse register, dead-code paths gated by a compile-time
        constant.

        The threshold is *fraction* covered, not zero-covered, because
        with seeded bias driving high baseline coverage many dead-
        extension signals end up with 1-2 incidentally-covered bins
        (peripheral writes through aliased structs). A signal with
        ≤5% of bins covered after the active growth phase is still
        dead for practical purposes; the few covered bits don't unlock
        the rest.

        Tying detection to coverage stall (rather than a fixed cycle
        threshold) gives BMC its full natural lifecycle to reach hard
        bins before we declare anything dead.

        Returns ``set()`` if coverage hasn't yet stalled, so this can
        be called unconditionally on every poll.
        """
        if self._cycle - self._last_new_native_cycle < stall_cycles:
            return set()
        rows = self._conn.execute(
            "SELECT json_extract(cpu.details_json, '$.reg') AS sig, "
            "       SUM(CASE WHEN cp.point_id IS NULL THEN 0 ELSE 1 END) AS covered, "
            "       COUNT(*) AS total "
            "FROM coverage_point_universe cpu "
            "LEFT JOIN coverage_points cp ON cp.point_id = cpu.point_id "
            "WHERE json_extract(cpu.details_json, '$.reg') IS NOT NULL "
            "GROUP BY sig"
        ).fetchall()
        out: set[str] = set()
        for sig, covered, total in rows:
            if not sig or not total:
                continue
            if covered / total <= max_covered_fraction:
                out.add(sig)
        return out

    def mark_signal_dead(self, signal: str) -> int:
        """Add every toggle bin associated with `signal` to the exhausted
        set so the BMC coverage-target picker skips them, AND record
        the signal name in ``_dead_signals`` so the register-target
        picker also skips registers on this signal. Returns the count
        of newly-exhausted bins (0 if all were already exhausted).

        The signal-level set is consulted by
        ``get_unvisited_neighbor_target`` to filter the iterated
        register list. Without this, the orchestrator's register-target
        fallback would keep picking dead-extension registers (e.g.
        accelerator regs with the acc coproc disabled), burning BMC
        budget on guaranteed timeouts.
        """
        if not signal:
            return 0
        self._dead_signals.add(signal)
        rows = self._conn.execute(
            "SELECT point_id FROM coverage_point_universe "
            "WHERE json_extract(details_json, '$.reg') = ?",
            (signal,)
        ).fetchall()
        n = 0
        for (pid,) in rows:
            if pid not in self._exhausted_coverage_points:
                self._exhausted_coverage_points.add(pid)
                n += 1
        return n

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
