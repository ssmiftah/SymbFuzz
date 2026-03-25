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


@dataclass
class DesignInfo:
    module_name: str
    verilog_path: str               # primary (first) source file
    inputs:       list[PortInfo]      = field(default_factory=list)
    outputs:      list[PortInfo]      = field(default_factory=list)
    registers:    list[RegisterInfo]  = field(default_factory=list)
    clock_port:   str                 = "clk"
    reset_port:   str | None          = None
    smt2_text:    str                 = ""
    verilog_files: list[str]          = field(default_factory=list)
    """All source files passed to Yosys (ordered: dependencies first, top last)."""

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
    """Return the path to sv2v if it is on PATH, else None."""
    for candidate in ("sv2v",):
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
    return info


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

    if not info.module_name:
        raise RuntimeError("SMT2 has no yosys-smt2-module annotation")

    # Pass 2: determine Bool vs BitVec return type from define-fun lines
    bool_map = _build_bool_map(smt2_text, info.module_name)

    # Pass 3: resolve arch_name from mangled sample_data names
    #   $auto$clk2fflogic...sample_data$/<signame>#sampled$N
    for mangled, width in raw_registers:
        arch_name = _extract_arch_name(mangled)
        if not arch_name:
            continue
        # Skip Yosys-internal nodes: valid RTL identifiers never contain
        # '$', '#', or ':'.  These appear when sv2v-generated intermediate
        # flip-flops get clk2fflogic'd (e.g. "sv2v_out.v:190$23_Y").
        if any(c in arch_name for c in ('$', '#', ':')):
            continue
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
