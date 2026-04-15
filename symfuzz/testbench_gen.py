"""
testbench_gen.py — Generate Vivado xsim simulation harness and UVM testbench
artifacts for a given design using Jinja2 templates.
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .design_parser import DesignInfo

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        keep_trailing_newline=True,
    )
    return env


def _is_reset_active_low(design: DesignInfo) -> bool:
    rp = design.reset_port or ""
    return rp.endswith(("_n", "_ni", "_b", "n", "b"))


def _ctx(design: DesignInfo, clock_period: str = "1 ps",
         native_coverage: bool = False,
         verilator_threads: int = 1) -> dict:
    reset_active_low = _is_reset_active_low(design)
    return {
        "module":              design.module_name,
        "clock_port":          design.clock_port,
        "clock_period":        clock_period,
        "reset_port":          design.reset_port,
        "reset_active_low":    reset_active_low,
        "registers":           design.registers,
        "enumerate_registers": list(enumerate(design.registers)),
        "data_inputs":         design.data_inputs,
        "all_inputs":          design.inputs,
        "outputs":             design.outputs,
        "verilog_path":        str(Path(design.verilog_path).resolve()),
        "verilog_files":       design.verilog_files or [str(Path(design.verilog_path).resolve())],
        "native_coverage":     native_coverage,
        "verilator_threads":   max(1, int(verilator_threads)),
    }


# --------------------------------------------------------------------------- #
# Vivado xsim harness generation                                               #
# --------------------------------------------------------------------------- #

def generate_vivado_harness(
    design: DesignInfo,
    output_dir: str | Path,
    clock_period: str = "1 ps",
    native_coverage: bool = False,
) -> Path:
    """
    Generate under <output_dir>/:
      tcl_bridge_<module>.tcl  — xsim stdin/stdout TCL bridge
      compile_<module>.sh      — xvlog + xelab compile script
    *clock_period* is the time argument passed to the TCL `run` command inside
    `clk_posedge`. Default `"1 ps"` minimizes per-cycle scheduler cost.
    Returns the output directory as a Path.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    env  = _jinja_env()
    ctx  = _ctx(design, clock_period=clock_period, native_coverage=native_coverage)
    m    = design.module_name

    bridge_src = env.get_template("tcl_bridge.tcl.j2").render(**ctx)
    (out / f"tcl_bridge_{m}.tcl").write_text(bridge_src)

    compile_src = env.get_template("compile.sh.j2").render(**ctx)
    compile_sh  = out / f"compile_{m}.sh"
    compile_sh.write_text(compile_src)
    compile_sh.chmod(0o755)

    return out


# --------------------------------------------------------------------------- #
# Verilator harness generation                                                 #
# --------------------------------------------------------------------------- #

def generate_verilator_harness(
    design: DesignInfo,
    output_dir: str | Path,
    native_coverage: bool = False,
    verilator_threads: int = 1,
) -> Path:
    """
    Generate under <output_dir>/verilator/:
      harness.cpp              — C++ REPL harness (same JSON protocol as xsim)
      compile_<module>.sh      — verilator --cc --build --exe script
    Returns the verilator/ subdirectory path.
    """
    out = Path(output_dir) / "verilator"
    out.mkdir(parents=True, exist_ok=True)

    env = _jinja_env()
    ctx = _ctx(design, native_coverage=native_coverage,
               verilator_threads=verilator_threads)
    m   = design.module_name

    harness_src = env.get_template("verilator_harness.cpp.j2").render(**ctx)
    (out / "harness.cpp").write_text(harness_src)

    compile_src = env.get_template("verilator_compile.sh.j2").render(**ctx)
    compile_sh  = out / f"compile_{m}.sh"
    compile_sh.write_text(compile_src)
    compile_sh.chmod(0o755)

    return out


# --------------------------------------------------------------------------- #
# UVM testbench generation (SV artifacts for commercial simulators)           #
# --------------------------------------------------------------------------- #

def generate_uvm_testbench(design: DesignInfo, output_dir: str | Path) -> Path:
    """
    Generate a full UVM testbench under <output_dir>/uvm/.
    Returns the uvm/ subdirectory path.
    """
    uvm_dir = Path(output_dir) / "uvm"
    uvm_dir.mkdir(parents=True, exist_ok=True)

    env = _jinja_env()
    ctx = _ctx(design)
    m   = design.module_name

    templates_and_files = [
        ("uvm_if.sv.j2",        f"{m}_if.sv"),
        ("uvm_seq_item.sv.j2",  f"{m}_seq_item.sv"),
        ("uvm_driver.sv.j2",    f"{m}_driver.sv"),
        ("uvm_monitor.sv.j2",   f"{m}_monitor.sv"),
        ("uvm_agent.sv.j2",     f"{m}_agent.sv"),
        ("uvm_env.sv.j2",       f"{m}_env.sv"),
        ("uvm_rand_seq.sv.j2",  f"{m}_rand_seq.sv"),
        ("uvm_bmc_seq.sv.j2",   f"{m}_bmc_seq.sv"),
        ("uvm_test.sv.j2",      f"{m}_test.sv"),
        ("uvm_tb_top.sv.j2",    f"tb_{m}_uvm.sv"),
    ]
    for tmpl_name, out_file in templates_and_files:
        src = env.get_template(tmpl_name).render(**ctx)
        (uvm_dir / out_file).write_text(src)

    return uvm_dir
