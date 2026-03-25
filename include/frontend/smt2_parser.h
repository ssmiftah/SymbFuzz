#pragma once
#include <string>
#include <vector>

namespace symbfuzz {

enum class PortDir { Input, Output, InOut };

struct PortInfo {
    std::string name;
    int         width;  // bit width (1 = single bit / bool)
    PortDir     dir;
};

enum class RegKind {
    Architectural,   // user-defined register (rare after clk2fflogic)
    SampleData,      // clk2fflogic data-sampler — the actual architectural state
    SampleControl,   // clk2fflogic clock/reset sampler — already initialised by mod_i
};

struct RegisterInfo {
    std::string name;        // full mangled name
    std::string arch_name;   // extracted architectural name — for SampleData only
    int         width;
    RegKind     kind;
    bool        returns_bool; // true when the SMT2 accessor returns Bool (not BitVec)
};

struct WireInfo {
    std::string name;
    int         width;
};

struct DesignModel {
    std::string          module_name;
    std::vector<PortInfo>     inputs;
    std::vector<PortInfo>     outputs;
    std::vector<RegisterInfo> registers;
    std::vector<WireInfo>     wires;    // -wires exposed internals
    std::string          smt2_text;    // verbatim yosys output (embedded in BMC query)
};

// Parse the annotation comments in a Yosys write_smt2 -wires output.
// Annotation lines look like:
//   ; yosys-smt2-module <name>
//   ; yosys-smt2-input  <name> <width>
//   ; yosys-smt2-output <name> <width>
//   ; yosys-smt2-register <name> <width>
//   ; yosys-smt2-wire <name> <width>
DesignModel parse_design_model(const std::string& smt2_text);

} // namespace symbfuzz
