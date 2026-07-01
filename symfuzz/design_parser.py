"""
design_parser.py — Extract port list and register info from a Verilog/SV design
using Yosys (proc; clk2fflogic; write_smt2 -wires) then parsing annotation comments.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------- #
# Data classes                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class PortInfo:
    name: str
    width: int          # bit-width
    direction: str      # 'input' | 'output'

    @property
    def mask(self) -> int:
        return (1 << self.width) - 1

    @property
    def verilator_ctype(self) -> str:
        if self.width <= 8:  return "CData"
        if self.width <= 16: return "SData"
        if self.width <= 32: return "IData"
        return "QData"


@dataclass
class RegisterInfo:
    arch_name: str      # RTL signal name (e.g. "count", "state")
    mangled_name: str   # full Yosys SMT2 name
    width: int
    returns_bool: bool  # True when the SMT2 accessor returns Bool (1-bit boolean)

    @property
    def verilator_ctype(self) -> str:
        """C++ type used by Verilator for this signal width."""
        if self.width <= 8:  return "CData"
        if self.width <= 16: return "SData"
        if self.width <= 32: return "IData"
        return "QData"

    @property
    def mask(self) -> int:
        return (1 << self.width) - 1

    @property
    def xsim_path(self) -> str:
        """Signal path for Vivado xsim get_value/set_value.
        Yosys uses dots for hierarchy (u_a.count); xsim uses slashes (u_a/count).
        """
        return self.arch_name.replace('.', '/')

    @property
    def verilator_path(self) -> str:
        """Relative verilator accessor path for this register (no ``rootp->``
        or top-module prefix). The template prepends
        ``rootp-><top_module>__DOT__`` when emitting the C++ expression.
        """
        return self.arch_name.replace('.', '__DOT__')


@dataclass
class DesignInfo:
    module_name: str
    verilog_path: str               # primary (first) source file
    inputs:       list[PortInfo]      = field(default_factory=list)
    outputs:      list[PortInfo]      = field(default_factory=list)
    registers:    list[RegisterInfo]  = field(default_factory=list)
    wires:        list[PortInfo]      = field(default_factory=list)
    """Internal (combinational) wires Yosys exposed in the SMT2 annotations.
    Used by the orchestrator to classify coverage targets — wires that
    aren't flop-backed get a reach-a-value BMC target rather than a flip
    constraint."""
    clock_port:   str                 = "clk"
    reset_port:   str | None          = None
    smt2_text:    str                 = ""
    verilog_files: list[str]          = field(default_factory=list)
    """All source files passed to Yosys (ordered: dependencies first, top last)."""
    case_labels:  dict[str, list[int]] = field(default_factory=dict)
    """Per-input-port set of literal values extracted from `case (...)`
    statements in the RTL source. Used by the adaptive input bias model
    to pre-credit known-interesting values at startup so the bias doesn't
    have to discover them from cold via coverage feedback. Empty when
    no `case` statements are found or extraction is skipped."""

    @property
    def data_inputs(self) -> list[PortInfo]:
        """Inputs excluding clock."""
        return [p for p in self.inputs if p.name != self.clock_port]

    @property
    def max_state_space(self) -> int:
        total = 1
        for r in self.registers:
            total *= (1 << r.width)
            if total > (1 << 24):   # cap at 16 M for display
                return total
        return total


# --------------------------------------------------------------------------- #
# Yosys runner                                                                 #
# --------------------------------------------------------------------------- #

_CLK_NAMES  = {"clk", "clock", "CLK", "CLOCK", "clk_i", "clk_o"}
_RST_NAMES  = {"rst", "reset", "rst_n", "resetn", "rstn", "rst_ni",
               "areset", "aresetn", "nreset", "sreset"}


def _find_sv2v() -> str | None:
    """Return the path to a working sv2v binary, else None.

    Tries (in order):
      1. SYMFUZZ_SV2V env var (explicit override).
      2. PATH (standard install location).
      3. The directory of the current Python interpreter — when
         symfuzz is invoked via ``.venv/bin/symfuzz`` without
         activating the venv, ``sys.executable`` lives in that same
         directory, so a sv2v binary placed alongside it is reachable
         without requiring the user to ``source activate``.
    """
    import sys
    override = os.environ.get("SYMFUZZ_SV2V")
    candidates: list[str] = []
    if override:
        candidates.append(override)
    candidates.append("sv2v")
    venv_bin = Path(sys.executable).resolve().parent / "sv2v"
    if str(venv_bin) not in candidates:
        candidates.append(str(venv_bin))
    for candidate in candidates:
        try:
            r = subprocess.run(
                [candidate, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return None


def _sv2v_convert(files: list[str], top_module: str | None, out_path: str) -> list[str]:
    """
    Run sv2v on *files* and write the single converted Verilog file to *out_path*.
    Returns ``[out_path]`` on success.  Raises RuntimeError on failure.

    The output is written to a caller-supplied path so it persists after any
    temporary directory is cleaned up (xsim needs it for compilation).
    """
    sv2v_bin = _find_sv2v()
    if sv2v_bin is None:
        raise RuntimeError(
            "sv2v not found on PATH. Install it to process SystemVerilog designs:\n"
            "  curl -L https://github.com/zachjs/sv2v/releases/latest/download/"
            "sv2v-Linux.zip -o sv2v-Linux.zip && unzip sv2v-Linux.zip && "
            "cp sv2v /usr/local/bin/sv2v"
        )

    cmd = [sv2v_bin, "--write=" + out_path] + files
    if top_module:
        cmd += ["--top=" + top_module]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"sv2v failed:\n{r.stderr}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("sv2v produced no output.")
    return [out_path]


def parse_design(
    verilog_files: "str | Path | list[str | Path]",
    top_module: str | None = None,
    flatten: bool = True,
    use_sv2v: bool = False,
) -> DesignInfo:
    """
    Run Yosys on one or more Verilog/SV source files, parse the SMT2
    annotation comments, and return a populated :class:`DesignInfo`.

    *verilog_files* may be a single path or an ordered list
    (dependencies first, top-level last).

    *use_sv2v* — when True, preprocess the sources with ``sv2v`` before
    handing them to Yosys.  Required for designs that use SystemVerilog
    constructs Yosys cannot parse (``parameter type``, struct casts, complex
    package functions).  ``sv2v`` must be installed and on PATH.
    """
    if isinstance(verilog_files, (str, Path)):
        files = [str(Path(verilog_files).resolve())]
    else:
        files = [str(Path(f).resolve()) for f in verilog_files]

    primary = files[0]

    # sv2v output goes to a local sv2v_cache/ directory (never into the
    # source tree).  Named after the top module so multiple designs coexist.
    cache_name = (top_module or Path(files[-1]).stem) + "__sv2v.v"
    sv2v_cache_dir = Path.cwd() / "sv2v_cache"
    sv2v_cache_dir.mkdir(exist_ok=True)
    sv2v_out = str(sv2v_cache_dir / cache_name)
    converted_files: list[str] | None = None

    with tempfile.TemporaryDirectory(prefix="symbfuzz_parse_") as tmpdir:
        smt2_path = os.path.join(tmpdir, "design.smt2")

        # ---- Optional sv2v preprocessing --------------------------------
        yosys_files = files
        if use_sv2v:
            converted_files = _sv2v_convert(files, top_module, sv2v_out)
            yosys_files = converted_files

        script_parts = [f"read_verilog -sv {f}" for f in yosys_files]
        script_parts.append(
            "hierarchy -check" + (f" -top {top_module}" if top_module else "")
        )
        if flatten:
            script_parts.append("flatten")
        script_parts += [
            "proc",
            "clk2fflogic",
            "opt",
            f"write_smt2 -wires {smt2_path}",
        ]
        script = "; ".join(script_parts)

        result = subprocess.run(
            ["yosys", "-p", script],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise RuntimeError(f"Yosys failed:\n{result.stderr}")

        if not os.path.exists(smt2_path):
            raise RuntimeError("Yosys succeeded but produced no SMT2 output.")

        with open(smt2_path) as f:
            smt2_text = f.read()

    info = _parse_smt2_annotations(smt2_text, primary)
    # When sv2v was used, xsim must compile the converted file (plain Verilog),
    # not the original SV sources which contain constructs xvlog may reject with
    # default parameter types.
    info.verilog_files = converted_files if converted_files is not None else files
    # Extract per-port case-statement labels for adaptive bias seeding.
    # Cheap regex scan over the Verilog the user gave us (or the sv2v
    # output, which is what Yosys saw). Failures are non-fatal.
    try:
        info.case_labels = _extract_case_labels(info.verilog_files, info.inputs)
    except Exception:
        info.case_labels = {}
    return info


# --------------------------------------------------------------------------- #
# Case-label extractor (for adaptive input bias seeding)                       #
# --------------------------------------------------------------------------- #

_CASE_BLOCK_RE = re.compile(
    r"\bcase\s*\(\s*([^)]+?)\s*\)(.+?)\bendcase\b",
    re.DOTALL,
)
_LABEL_LINE_RE = re.compile(
    r"^\s*((?:(?:\d+\s*'\s*[bhdBHD]\s*[0-9a-fA-F_xX?]+|\d+)\s*(?:,\s*)?)+)\s*:",
    re.MULTILINE,
)
_SIZED_LIT_RE = re.compile(
    r"(\d+)\s*'\s*([bhdBHD])\s*([0-9a-fA-F_xX?]+)"
)
_PORT_SUFFIXES = ("_i", "_in", "_input", "_o", "_out", "_q", "_d")


def _port_stem(name: str) -> str:
    for suf in _PORT_SUFFIXES:
        if name.endswith(suf):
            return name[:-len(suf)]
    return name


def _extract_case_labels(
    verilog_files: list[str],
    inputs: list[PortInfo],
) -> dict[str, list[int]]:
    """Scan Verilog source for `case (X) ... endcase` blocks; extract
    integer labels and associate them with input ports by name match
    (direct or via stem-substring against the case discriminator).

    Returns a dict mapping port_name → sorted list of distinct values.
    Conservative on width: only includes sized labels whose declared
    width equals the port width — this avoids polluting wide ports
    with labels from sub-range case discriminators.

    Used to pre-credit the adaptive bias model (see ``input_bias.py``)
    so it exploits known-valid values from cycle 1 instead of slowly
    learning them via coverage feedback. Design-agnostic — any RTL
    with `case` statements on input-derived signals benefits.
    """
    port_by_name = {p.name: p for p in inputs}
    port_stems = [(_port_stem(p.name), p) for p in inputs]
    # Sort stems longest-first so substring matches prefer the most
    # specific port name (e.g. "csr_addr" wins over "csr").
    port_stems.sort(key=lambda kv: -len(kv[0]))
    result: dict[str, set[int]] = {p.name: set() for p in inputs}

    for path in verilog_files:
        try:
            text = open(path).read()
        except Exception:
            continue
        # Strip comments to keep them from generating false matches.
        text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)

        for case_m in _CASE_BLOCK_RE.finditer(text):
            disc = case_m.group(1).strip()
            body = case_m.group(2)

            # Match the discriminator to an input port:
            # (1) direct name equality
            # (2) port stem appears as a substring in the discriminator
            matched: "PortInfo | None" = None
            if disc in port_by_name:
                matched = port_by_name[disc]
            else:
                for stem, p in port_stems:
                    if stem and stem in disc:
                        matched = p
                        break
            if matched is None:
                continue

            pw = matched.width
            mask = matched.mask

            for lc in _LABEL_LINE_RE.finditer(body):
                for part in lc.group(1).split(","):
                    part = part.strip()
                    sm = _SIZED_LIT_RE.fullmatch(part)
                    if not sm:
                        continue
                    try:
                        width = int(sm.group(1))
                    except ValueError:
                        continue
                    if width != pw:
                        continue
                    digits = sm.group(3).replace("_", "").lower()
                    if "x" in digits or "?" in digits:
                        continue
                    base = {"h": 16, "b": 2, "d": 10}[sm.group(2).lower()]
                    try:
                        val = int(digits, base) & mask
                    except ValueError:
                        continue
                    result[matched.name].add(val)

    # Drop empty entries and freeze as sorted lists.
    return {p: sorted(vs) for p, vs in result.items() if vs}


# --------------------------------------------------------------------------- #
# SMT2 annotation parser                                                       #
# --------------------------------------------------------------------------- #

def _parse_smt2_annotations(smt2_text: str, verilog_path: str) -> DesignInfo:
    info = DesignInfo(
        module_name="", verilog_path=verilog_path, smt2_text=smt2_text,
        verilog_files=[verilog_path],
    )

    # Pass 1: collect annotation lines
    raw_registers: list[tuple[str, int]] = []   # (mangled_name, width)

    for line in smt2_text.splitlines():
        if not line.startswith("; yosys-smt2-"):
            continue
        parts = line[2:].split()   # skip leading "; "
        tag = parts[0] if parts else ""

        if tag == "yosys-smt2-module":
            info.module_name = parts[1] if len(parts) > 1 else ""

        elif tag == "yosys-smt2-input":
            name  = parts[1] if len(parts) > 1 else ""
            width = int(parts[2]) if len(parts) > 2 else 1
            info.inputs.append(PortInfo(name=name, width=width, direction="input"))

        elif tag == "yosys-smt2-output":
            name  = parts[1] if len(parts) > 1 else ""
            width = int(parts[2]) if len(parts) > 2 else 1
            info.outputs.append(PortInfo(name=name, width=width, direction="output"))

        elif tag == "yosys-smt2-register":
            name  = parts[1] if len(parts) > 1 else ""
            width = int(parts[2]) if len(parts) > 2 else 1
            if "sample_data" in name:
                raw_registers.append((name, width))
            # skip sample_control / other auto registers

        elif tag == "yosys-smt2-wire":
            # Internal combinational wires. Yosys emits these for every
            # named wire in the flattened netlist. We track their names +
            # widths so the orchestrator can classify Verilator coverage
            # points as flop-backed (use flip target) vs combinational
            # (use reach-value target).
            name  = parts[1] if len(parts) > 1 else ""
            width = int(parts[2]) if len(parts) > 2 else 1
            if name and not any(w.name == name for w in info.wires):
                info.wires.append(PortInfo(name=name, width=width,
                                           direction="wire"))

    if not info.module_name:
        raise RuntimeError("SMT2 has no yosys-smt2-module annotation")

    # Pass 2: determine Bool vs BitVec return type from define-fun lines
    bool_map = _build_bool_map(smt2_text, info.module_name)

    # Pass 3: resolve arch_name from mangled sample_data names
    #   $auto$clk2fflogic...sample_data$/<signame>#sampled$N
    #
    # clk2fflogic emits two sample_data registers per flop (negedge +
    # posedge of the 2-transitions-per-cycle model), so the same arch_name
    # shows up under different `$N` suffixes. Dedup by arch_name and keep
    # only the first occurrence — they represent the same logical storage.
    seen_arch_names: set[str] = set()
    for mangled, width in raw_registers:
        arch_name = _extract_arch_name(mangled)
        if not arch_name:
            continue
        # Skip Yosys-internal nodes: valid RTL identifiers never contain
        # '$', '#', ':'. They also can't contain '[', ']', '{', '}' for our
        # purposes — Yosys sometimes emits packed-array element names like
        # "vsstatus_d[1]}" that break the harness's force/read_state paths.
        if any(c in arch_name for c in ('$', '#', ':', '[', ']', '{', '}')):
            continue
        # Skip sv2v procedural-variable artifacts: sv2v converts SV
        # always-block local `reg`s into named blocks like
        # "sv2v_autoblock_<N>.<var>" that Yosys reports as registers but
        # Verilator folds away. They're not addressable at runtime, so
        # force/read_state would fail. Identify by the autoblock prefix.
        if "sv2v_autoblock" in arch_name or "sv2v_tmp_" in arch_name:
            continue
        # Skip registers wider than 64 bits — the JSON force/read_state
        # protocol and the Verilator harness template use uint64_t. Wider
        # registers come from packed arrays (e.g. pmpaddr_q[64][54]) that
        # Yosys flattens. Coverage on these still tracks toggle bins; we
        # just can't force them directly.
        if width > 64:
            continue
        if arch_name in seen_arch_names:
            continue
        seen_arch_names.add(arch_name)
        returns_bool = bool_map.get(mangled, width == 1)
        info.registers.append(RegisterInfo(
            arch_name=arch_name,
            mangled_name=mangled,
            width=width,
            returns_bool=returns_bool,
        ))

    # Identify clock and reset ports
    for p in info.inputs:
        if p.name in _CLK_NAMES:
            info.clock_port = p.name
            break

    for p in info.inputs:
        if p.name in _RST_NAMES:
            info.reset_port = p.name
            break

    return info


def _extract_arch_name(mangled: str) -> str:
    """Extract signal name from $auto$clk2fflogic...sample_data$/<name>#sampled$N"""
    slash = mangled.rfind('/')
    hash_ = mangled.rfind('#sampled')
    if slash != -1 and hash_ != -1 and hash_ > slash:
        return mangled[slash + 1 : hash_]
    return ""


def _build_bool_map(smt2_text: str, module: str) -> dict[str, bool]:
    """
    Scan define-fun lines to build a map: mangled_accessor_name → returns_bool.
    Yosys emits: (define-fun |mod_n <name>| ((state |mod_s|)) Bool ...)
    """
    prefix = f"(define-fun |{module}_n "
    result: dict[str, bool] = {}

    for line in smt2_text.splitlines():
        if not line.startswith(prefix):
            continue
        # signal name is between prefix and the next "|"
        name_start = len(prefix)
        name_end = line.find('|', name_start)
        if name_end == -1:
            continue
        sig_name = line[name_start:name_end]

        # return type appears after ")) "
        paren_close = line.find(')) ', name_end)
        if paren_close == -1:
            continue
        ret_type = line[paren_close + 3:]
        result[sig_name] = ret_type.startswith("Bool")

    return result
