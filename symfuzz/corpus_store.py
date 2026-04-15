"""
corpus_store.py — Unified checkpoint + AFL-style input corpus.

Durable tier:  RLE-compressed input sequences, one SQLite row per frontier state.
               Replay = driver.reset() + stepping each segment repeat times.
               Survives RTL recompilation.

Cache tier:    xsim .wdb files keyed by (state_hash, design_hash). O(100 ms)
               restore. Wholesale-invalidated on design-hash mismatch.

Recording  :   CorpusStore.append_input(segments, inputs) RLE-extends a list of
               RLESegment tuples. Orchestrator feeds every applied input dict.

Insertion  :   CorpusStore.add_entry(state_hash, segments, source, driver) writes
               the SQLite row and optionally writes a .wdb if the current mode
               and min-depth threshold permit.

Restoration:   CorpusStore.restore(state_hash, driver) tries .wdb first if the
               mode allows and the design hash matches; falls back to replaying
               the RLE sequence from reset; optionally rebuilds the .wdb cache
               on fallback. Validated by post-restore read_state() comparison.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .coverage_db import state_hash as compute_state_hash
from .design_parser import DesignInfo
from .sim_driver import SimDriver


RESTORE_MODES = ("auto", "replay", "xsim", "off")


@dataclass
class RLESegment:
    inputs: dict[str, int]
    repeat: int


@dataclass
class CorpusEntry:
    state_hash:    int
    segments:      list[RLESegment]
    total_depth:   int
    source:        str
    wdb_path:      Optional[str]
    hit_count:     int
    contribution:  int = 1


def append_input(segments: list[RLESegment], inputs: dict[str, int]) -> None:
    """RLE-extend *segments* in place with one more cycle of *inputs*."""
    if segments and segments[-1].inputs == inputs:
        segments[-1].repeat += 1
    else:
        segments.append(RLESegment(inputs=dict(inputs), repeat=1))


def _segments_to_json(segments: list[RLESegment]) -> str:
    return json.dumps([[s.inputs, s.repeat] for s in segments],
                      sort_keys=True, separators=(',', ':'))


def _segments_from_json(blob: str) -> list[RLESegment]:
    return [RLESegment(inputs=inp, repeat=rep)
            for inp, rep in json.loads(blob)]


def _expand_segments(segments: list[RLESegment]) -> Iterable[dict[str, int]]:
    for seg in segments:
        for _ in range(seg.repeat):
            yield seg.inputs


class CorpusStore:
    """SQLite-backed corpus + xsim .wdb cache."""

    def __init__(
        self,
        db_path:        str | Path,
        wdb_dir:        str | Path,
        design:         DesignInfo,
        snapshot_file:  Optional[Path],
        mode:           str   = "replay",
        wdb_min_depth:  int   = 20,
        wdb_budget_mb:  int   = 500,
        verbose:        bool  = False,
    ):
        if mode not in RESTORE_MODES:
            raise ValueError(f"invalid restore mode: {mode}")
        # xsim 2025.2 does not expose simulator-state checkpoint/restore (only
        # checkpoint_vcd for waveforms). Tier 2 is therefore disabled; 'auto'
        # and 'xsim' modes silently downgrade to 'replay'.
        if mode in ("auto", "xsim"):
            if verbose:
                print(f"[corpus] mode '{mode}' downgraded to 'replay' "
                      f"(xsim lacks state checkpoint support)")
            mode = "replay"
        self.design        = design
        self.mode          = mode
        self.wdb_min_depth = wdb_min_depth
        self.wdb_budget_mb = wdb_budget_mb
        self.verbose       = verbose

        self.wdb_dir = Path(wdb_dir).resolve()
        self.wdb_dir.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(db_path))
        # Schema is created by CoverageDB; we just read/write the table.

        self.design_hash = self._compute_design_hash(snapshot_file)
        # Only relevant when Tier 2 is enabled — currently a no-op under 'replay'.
        self._check_meta_and_invalidate_wdbs_if_stale()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def add_entry(
        self,
        state_h:      int,
        segments:     list[RLESegment],
        source:       str,
        driver:       SimDriver,
        contribution: int = 1,
    ) -> None:
        if self.mode == "off":
            return
        if not segments:
            return

        total_depth   = sum(s.repeat for s in segments)
        segments_json = _segments_to_json(segments)

        # Write / replace SQLite row. Keep the best (deepest) entry on collision.
        existing = self._fetch(state_h)
        if existing and existing.total_depth <= total_depth:
            # Existing path is at least as short — keep it.
            return

        wdb_path = None
        if self._should_write_wdb(total_depth):
            wdb_path = str(self._wdb_path_for(state_h))
            try:
                driver.checkpoint(wdb_path)
            except RuntimeError as e:
                if self.verbose:
                    print(f"[corpus] checkpoint failed for {state_h}: {e}")
                wdb_path = None

        contribution_val = max(1, int(contribution))
        self._conn.execute(
            """INSERT INTO corpus_entries
               (state_hash, segments_json, total_depth, source, wdb_path,
                hit_count, created, contribution_score)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?)
               ON CONFLICT(state_hash) DO UPDATE SET
                   segments_json      = excluded.segments_json,
                   total_depth        = excluded.total_depth,
                   source             = excluded.source,
                   wdb_path           = COALESCE(excluded.wdb_path, corpus_entries.wdb_path),
                   contribution_score = MAX(corpus_entries.contribution_score,
                                            excluded.contribution_score)""",
            (state_h, segments_json, total_depth, source, wdb_path,
             time.monotonic(), contribution_val),
        )
        self._conn.commit()

        if wdb_path is not None:
            self._evict_lru_wdb()

    def restore(self, state_h: int, driver: SimDriver) -> bool:
        """
        Two-tier restore. Returns True on success. Verifies post-restore state
        hash matches; on mismatch, evicts the stale row and returns False.
        """
        if self.mode == "off":
            return False

        entry = self._fetch(state_h)
        if entry is None:
            return False

        # -------- Tier 2: xsim .wdb -------------------------------------
        if self.mode in ("auto", "xsim") and entry.wdb_path:
            wdb_ok = self._try_wdb_restore(entry, driver)
            if wdb_ok:
                return self._verify_and_evict_on_mismatch(entry, driver)

        # -------- Tier 1: RLE replay (single batched call) --------------
        try:
            driver.reset(cycles=5)
            inputs_list = []
            for inputs in _expand_segments(entry.segments):
                full = {p.name: 0 for p in self.design.data_inputs}
                full.update(inputs)
                inputs_list.append(full)
            if inputs_list:
                _, err = driver.step_trace(inputs_list)
                if err is not None:
                    if self.verbose:
                        print(f"[corpus] replay restore error for {state_h}: {err}")
                    return False
        except RuntimeError as e:
            if self.verbose:
                print(f"[corpus] replay restore failed for {state_h}: {e}")
            return False

        ok = self._verify_and_evict_on_mismatch(entry, driver)
        if not ok:
            return False

        # Rebuild .wdb cache under 'auto' mode.
        if self.mode == "auto" and not entry.wdb_path and self._should_write_wdb(entry.total_depth):
            wdb_path = str(self._wdb_path_for(state_h))
            try:
                driver.checkpoint(wdb_path)
                self._conn.execute(
                    "UPDATE corpus_entries SET wdb_path=? WHERE state_hash=?",
                    (wdb_path, state_h),
                )
                self._conn.commit()
                self._evict_lru_wdb()
            except RuntimeError as e:
                if self.verbose:
                    print(f"[corpus] wdb rebuild failed: {e}")

        return True

    def has(self, state_h: int) -> bool:
        return self._fetch(state_h) is not None

    def sample(self, rng: random.Random, uniform: bool = False) -> Optional[CorpusEntry]:
        """
        Sample a corpus entry and increment its hit_count.

        Weighted (default): weight = contribution_score / (1 + hit_count).
        Entries with bigger contribution (more new coverage at insertion)
        and fewer prior samples win. With all scores == 1 and hit_counts
        == 0 this is equivalent to uniform picking — a useful parity
        guarantee for register-tuple-only campaigns.

        Pass *uniform=True* to force the legacy `rng.choice` path (used
        by the --uniform-sampling A/B kill-switch).
        """
        rows = self._conn.execute(
            "SELECT state_hash, segments_json, total_depth, source, "
            "       wdb_path, hit_count, contribution_score "
            "FROM corpus_entries"
        ).fetchall()
        if not rows:
            return None
        if uniform:
            row = rng.choice(rows)
        else:
            weights = [max(1, r[6]) / (1 + max(0, r[5])) for r in rows]
            row = rng.choices(rows, weights=weights, k=1)[0]
        self._conn.execute(
            "UPDATE corpus_entries SET hit_count = hit_count + 1 WHERE state_hash = ?",
            (row[0],),
        )
        self._conn.commit()
        return CorpusEntry(
            state_hash=row[0],
            segments=_segments_from_json(row[1]),
            total_depth=row[2],
            source=row[3],
            wdb_path=row[4],
            hit_count=row[5] + 1,
            contribution=row[6],
        )

    def pick_deepest(self) -> Optional[CorpusEntry]:
        """Return the entry with the largest total_depth, or None if empty."""
        row = self._conn.execute(
            "SELECT state_hash, segments_json, total_depth, source, "
            "       wdb_path, hit_count, contribution_score "
            "FROM corpus_entries "
            "ORDER BY total_depth DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return CorpusEntry(
            state_hash=row[0],
            segments=_segments_from_json(row[1]),
            total_depth=row[2],
            source=row[3],
            wdb_path=row[4],
            hit_count=row[5],
            contribution=row[6],
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    def _fetch(self, state_h: int) -> Optional[CorpusEntry]:
        row = self._conn.execute(
            "SELECT state_hash, segments_json, total_depth, source, "
            "       wdb_path, hit_count, contribution_score "
            "FROM corpus_entries WHERE state_hash = ?",
            (state_h,),
        ).fetchone()
        if row is None:
            return None
        return CorpusEntry(
            state_hash=row[0],
            segments=_segments_from_json(row[1]),
            total_depth=row[2],
            source=row[3],
            wdb_path=row[4],
            hit_count=row[5],
            contribution=row[6],
        )

    def _should_write_wdb(self, total_depth: int) -> bool:
        if self.mode in ("off", "replay"):
            return False
        if total_depth < self.wdb_min_depth:
            return False
        return True

    def _wdb_path_for(self, state_h: int) -> Path:
        # Signed → unsigned hex for a tidy filename
        name = f"{state_h & 0xFFFFFFFFFFFFFFFF:016x}.wdb"
        return self.wdb_dir / name

    def _try_wdb_restore(self, entry: CorpusEntry, driver: SimDriver) -> bool:
        if not entry.wdb_path or not os.path.exists(entry.wdb_path):
            return False
        try:
            driver.restore(entry.wdb_path)
            return True
        except RuntimeError as e:
            if self.verbose:
                print(f"[corpus] .wdb restore failed, falling back to replay: {e}")
            return False

    def _verify_and_evict_on_mismatch(
        self, entry: CorpusEntry, driver: SimDriver
    ) -> bool:
        try:
            observed = driver.read_state()
        except RuntimeError:
            return False
        observed_hash = compute_state_hash(observed)
        if observed_hash == entry.state_hash:
            return True
        if self.verbose:
            seg_summary = [(dict(s.inputs), s.repeat) for s in entry.segments]
            print(f"[corpus] state-hash mismatch after restore "
                  f"(entry depth={entry.total_depth}, source={entry.source}, "
                  f"observed={observed}, segs={seg_summary}); evicting")
        self._evict_row(entry.state_hash)
        return False

    def _evict_row(self, state_h: int) -> None:
        row = self._conn.execute(
            "SELECT wdb_path FROM corpus_entries WHERE state_hash = ?",
            (state_h,),
        ).fetchone()
        if row and row[0]:
            try:
                os.remove(row[0])
            except FileNotFoundError:
                pass
        self._conn.execute(
            "DELETE FROM corpus_entries WHERE state_hash = ?", (state_h,)
        )
        self._conn.commit()

    def _evict_lru_wdb(self) -> None:
        budget_bytes = self.wdb_budget_mb * 1024 * 1024
        rows = self._conn.execute(
            "SELECT state_hash, wdb_path, created FROM corpus_entries "
            "WHERE wdb_path IS NOT NULL"
        ).fetchall()
        files = []
        total = 0
        for sh, wp, created in rows:
            try:
                sz = os.path.getsize(wp)
            except OSError:
                continue
            files.append((sh, wp, created, sz))
            total += sz
        # Evict oldest-created .wdb until under budget. Keep the SQLite row
        # but null out wdb_path so the Tier-1 sequence stays usable.
        files.sort(key=lambda t: t[2])
        i = 0
        while total > budget_bytes and i < len(files):
            sh, wp, _, sz = files[i]
            try:
                os.remove(wp)
            except FileNotFoundError:
                pass
            self._conn.execute(
                "UPDATE corpus_entries SET wdb_path = NULL WHERE state_hash = ?",
                (sh,),
            )
            total -= sz
            i += 1
        if i > 0:
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # Design-hash meta                                                     #
    # ------------------------------------------------------------------ #

    def _compute_design_hash(self, snapshot_file: Optional[Path]) -> str:
        if snapshot_file is None or not Path(snapshot_file).exists():
            return "unknown"
        h = hashlib.sha256()
        with open(snapshot_file, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:16]

    def _check_meta_and_invalidate_wdbs_if_stale(self) -> None:
        meta_path = self.wdb_dir / "meta.json"
        stale = False
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                if meta.get("design_hash") != self.design_hash:
                    stale = True
            except Exception:
                stale = True
        else:
            stale = True  # first run or dir without meta

        if stale:
            # Wipe all .wdb files and clear wdb_path column; keep RLE rows.
            removed = 0
            for f in self.wdb_dir.glob("*.wdb"):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
            # Also clear any xsim-internal subdirs from checkpoint side-effects
            for f in self.wdb_dir.glob("*.wdb.dir"):
                shutil.rmtree(f, ignore_errors=True)
            self._conn.execute(
                "UPDATE corpus_entries SET wdb_path = NULL WHERE wdb_path IS NOT NULL"
            )
            self._conn.commit()
            meta_path.write_text(json.dumps({"design_hash": self.design_hash}))
            if self.verbose and removed:
                print(f"[corpus] design hash changed — wiped {removed} stale .wdb files "
                      f"(Tier 1 RLE sequences preserved)")
