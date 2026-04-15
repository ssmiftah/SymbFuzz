"""
drivers/xsim.py — Vivado xsim backend.

Spawns `xsim <snapshot> --tclbatch <bridge>` and communicates via the shared
JSON-over-stdio protocol (see drivers/base.py).
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from .base import BaseStdioDriver, CoverageSnapshot


class XsimDriver(BaseStdioDriver):
    backend_name = "xsim"

    def __init__(self, design, tb_dir, verbose: bool = False,
                 grey_bit_samples: int = 4):
        super().__init__(design, tb_dir, verbose)
        self.grey_bit_samples = max(0, int(grey_bit_samples))

    def collect_coverage(self) -> Optional[CoverageSnapshot]:
        """
        Flush the live xsim coverage DB to disk (via the TCL bridge), then
        run ``xcrg`` to produce an HTML report and scrape the aggregate
        percentages. xsim's code-coverage export is HTML-only in 2025.2.

        Side effect: updates ``self.last_cov_duration_ms`` / totals so the
        orchestrator progress logger can report per-poll cost.
        """
        t0 = time.monotonic()
        resp = self._send({"cmd": "coverage"})
        if "error" in resp:
            if self.verbose:
                print(f"[xsim] coverage error: {resp['error']}")
            self._record_cov_timing(t0)
            return None
        cov_dir = resp.get("cov_dir")
        if not cov_dir:
            self._record_cov_timing(t0)
            return None
        snap = _run_xcrg_and_scrape(cov_dir, design=self.design,
                                    verbose=self.verbose,
                                    grey_bit_samples=self.grey_bit_samples)
        self._record_cov_timing(t0)
        return snap

    def _record_cov_timing(self, t0: float) -> None:
        dur_ms = (time.monotonic() - t0) * 1000.0
        self.last_cov_duration_ms = dur_ms
        self.total_cov_duration_ms = getattr(self, "total_cov_duration_ms", 0.0) + dur_ms
        self.total_cov_calls = getattr(self, "total_cov_calls", 0) + 1
        if self.verbose:
            print(f"[timing] xcrg flush+parse {dur_ms:.0f}ms")

    def compile(self) -> None:
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

    # ------------------------------------------------------------------ #
    # Subprocess lifecycle                                                 #
    # ------------------------------------------------------------------ #

    def load(self) -> None:
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


# ---------------------------------------------------------------------- #
# xcrg wrapper — xsim's code-coverage export uses xcrg + HTML/stdout       #
# ---------------------------------------------------------------------- #

# xcrg prints aggregate scores on stdout, e.g.:
#     Line Coverage Score 100
#     Toggle Coverage Score  87
# We scrape those and synthesize point IDs proportional to the percentage
# so the CoverageDB point-union grows monotonically as coverage rises.

_XCRG_SCORE_RE = re.compile(
    r"(Statement|Line|Branch|Condition|Toggle|FSM\s*State|FSM\s*Transition)"
    r"\s+Coverage\s+Score\s+([\d.]+)",
    re.IGNORECASE,
)


def _run_xcrg_and_scrape(cov_dir: str, design=None, verbose: bool = False,
                         grey_bit_samples: int = 4) -> Optional[CoverageSnapshot]:
    cov_dir_p = Path(cov_dir)
    if not cov_dir_p.exists():
        return None
    excl = cov_dir_p / "empty_exclusion.cc"
    if not excl.exists():
        excl.write_text("")
    # xcrg silently ignores absolute -report_dir values. Must be run from
    # the cov_db_dir with relative paths to produce HTML output.
    cmd = [
        "xcrg",
        "-cov_db_dir", ".",
        "-report_dir", "./report",
        "-ccExclusionFile", "empty_exclusion.cc",
        "-nolog",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                           cwd=str(cov_dir_p))
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        if verbose:
            print(f"[xsim] xcrg failed: {e}")
        return None
    if r.returncode != 0:
        if verbose:
            print(f"[xsim] xcrg returncode={r.returncode}: {r.stderr[:200]}")
        return None

    snap = CoverageSnapshot()

    # Primary path: parse per-signal toggle detail from mod*.html. Falls
    # back to aggregate-percentage scaling if HTML parse fails.
    report_root = cov_dir_p / "report" / "codeCoverageReport"
    html_hit = _scrape_mod_toggle_html(report_root, snap,
                                       design=design, verbose=verbose,
                                       grey_bit_samples=grey_bit_samples)

    # Always also capture aggregate scores for the final banner (cheap).
    SCALE = 100
    for m in _XCRG_SCORE_RE.finditer(r.stdout):
        cat = m.group(1).lower().replace(" ", "_")
        pct = float(m.group(2))
        covered = int(round(pct / 100.0 * SCALE))
        if html_hit:
            # Skip synthetic toggle/line points when we have real IDs
            continue
        for i in range(covered):
            pid = f"xsim_{cat}_{i}"
            snap.points.add(pid)
            snap.kinds[pid] = cat
            snap.universe.add(pid)
        snap.total = (snap.total or 0) + SCALE

    # If HTML parse produced real points, derive total from the universe.
    if html_hit and snap.total is None:
        snap.total = len(snap.universe)

    if not snap.points and not snap.universe and snap.total is None:
        return None
    return snap


# Per-row format in xcrg's toggleCoverageTable:
#   <tr class='lineTableHoverGreen|Red|Grey'>
#     <td ...>line</td>
#     <td ...>var_type</td>
#     <td ...> <pre class='...'> All bits of <sig>  </pre> </td>     (or "Bit N of <sig>", "All Other bits of <sig>")
#     <td ...> <pre class='...'> <rise-count> </pre> </td>
#     <td ...> <pre class='...'> <fall-count> </pre> </td>
#   </tr>

_TOGGLE_ROW_RE = re.compile(
    r"<tr\s+class='lineTableHover(Green|Red|Grey)'>"
    r".*?<td[^>]*>(\d+)</td>"                              # line
    r".*?<td[^>]*>([^<]*)</td>"                            # var type
    r".*?<pre[^>]*>\s*([^<]*?)\s*</pre>"                   # variable descriptor
    r".*?<pre[^>]*>\s*(\d+)\s*</pre>"                      # rise count
    r".*?<pre[^>]*>\s*(\d+)\s*</pre>"                      # fall count
    r".*?</tr>",
    re.DOTALL,
)

_VAR_DESC_RE = re.compile(
    r"^(?:All bits of|All Other bits of|Bit\s+(\d+)\s+of)\s+(\w[\w\.\[\]]*)$"
)


def _lookup_sig_width(design, sig: str) -> Optional[int]:
    """Resolve signal width from a DesignInfo (registers / inputs / outputs).
    Returns None if the signal isn't known — caller falls back to ``bit='*'``."""
    if design is None:
        return None
    for r in getattr(design, "registers", []) or []:
        if r.arch_name == sig:
            return int(r.width)
    for p in getattr(design, "inputs", []) or []:
        if p.name == sig:
            return int(p.width)
    for p in getattr(design, "outputs", []) or []:
        if p.name == sig:
            return int(p.width)
    return None


def _emit_toggle_point(snap: CoverageSnapshot, module: str, sig: str,
                      bit, direction: str, covered: bool) -> None:
    pid = f"xtgl:{module}:{sig}:{bit}:{direction}"
    snap.universe.add(pid)
    snap.kinds[pid] = "toggle"
    snap.details[pid] = {"module": module, "reg": sig,
                         "bit": bit, "dir": direction}
    if covered:
        snap.points.add(pid)


def _scrape_mod_toggle_html(report_root: Path, snap: CoverageSnapshot,
                            design=None, verbose: bool = False,
                            grey_bit_samples: int = 4) -> bool:
    """
    Walk mod*.html files in the xcrg report directory and populate
    *snap.universe* / *snap.points* / *snap.details* with real xtgl IDs.

    "All bits of <sig>" and "All Other bits of <sig>" rows are expanded
    per-bit using the signal's width from *design*. Rows for signals whose
    width isn't resolvable fall back to the legacy ``bit='*'`` form so
    unknown wires don't regress.

    Covered vs uncovered granularity is aggregate at row level (xcrg HTML
    doesn't expose per-bit hit counts). For "All bits" rows:
        - row covered (Green)  → every expanded bit-point is covered
        - row uncovered (Red)  → every bit-point is uncovered
        - row partial  (Grey)  → every bit-point marked uncovered; the
                                  retire-on-unproductive path culls the
                                  ones that turn out to be already hit.

    Returns True iff at least one structured toggle row was parsed.
    """
    if not report_root.exists():
        return False
    any_hit = False
    missing_width_logged = 0
    for html_path in sorted(report_root.glob("mod*.html")):
        try:
            text = html_path.read_text(errors="ignore")
        except OSError:
            continue
        mod_match = re.search(
            r"Toggle Coverage of Module \d+ : (\w+)", text)
        module = mod_match.group(1) if mod_match else html_path.stem
        start = text.find("toggleCoverageTable")
        if start < 0:
            continue
        region = text[start:]
        for m in _TOGGLE_ROW_RE.finditer(region):
            klass, _lineno, _vtype, var_desc, rise_c, fall_c = m.groups()
            d = _VAR_DESC_RE.match(var_desc.strip())
            if not d:
                continue
            bit_str, sig = d.groups()
            any_hit = True

            # Row coverage status. Green = all bits covered in both dirs;
            # Red = none; Grey = partial. We track per-direction using the
            # count columns for the single-bit case but row-aggregate for
            # the "All bits" expansion where xcrg doesn't split them out.
            row_covered = (klass == "Green")

            if bit_str is not None:
                # Explicit "Bit N of <sig>" row — single per-bit point.
                # Use direction-specific counts since they're distinguishable.
                rise_covered = int(rise_c) > 0
                fall_covered = int(fall_c) > 0
                _emit_toggle_point(snap, module, sig, bit_str, "rise", rise_covered)
                _emit_toggle_point(snap, module, sig, bit_str, "fall", fall_covered)
                continue

            # "All bits of <sig>" / "All Other bits of <sig>" — expand.
            width = _lookup_sig_width(design, sig)
            if width is None:
                if verbose and missing_width_logged < 5:
                    print(f"[xsim] width unknown for '{sig}' — "
                          f"emitting single bit='*' point")
                    missing_width_logged += 1
                # Fallback path: single aggregate point per direction.
                _emit_toggle_point(snap, module, sig, "*", "rise",
                                   row_covered and int(rise_c) > 0)
                _emit_toggle_point(snap, module, sig, "*", "fall",
                                   row_covered and int(fall_c) > 0)
                continue

            # Pick the per-bit indices to emit. Green/Red rows are reliable
            # at the aggregate level so we expand fully. Grey rows lie about
            # individual bit status — we only sample a stratified subset of
            # bits to keep the universe small and the streak gate honest.
            if klass == "Grey" and grey_bit_samples > 0:
                step = max(1, width // max(1, grey_bit_samples))
                bits_to_emit = list(range(0, width, step))[:grey_bit_samples]
                if not bits_to_emit:
                    bits_to_emit = [0]
            else:
                bits_to_emit = range(width)
            for bit in bits_to_emit:
                _emit_toggle_point(snap, module, sig, bit, "rise", row_covered)
                _emit_toggle_point(snap, module, sig, bit, "fall", row_covered)
    return any_hit
