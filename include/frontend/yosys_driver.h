#pragma once
#include <string>
#include <vector>
#include <stdexcept>

namespace symbfuzz {

struct YosysResult {
    std::string smt2_text;   // full content of the generated .smt2 file
    std::string yosys_log;   // captured stdout/stderr from yosys
};

struct YosysConfig {
    // Ordered list of Verilog/SV source files (dependencies first, top last).
    // The legacy single-file field is kept as a convenience alias:
    // setting verilog_path fills verilog_files[0] when verilog_files is empty.
    std::vector<std::string> verilog_files;
    std::string verilog_path;  // legacy alias — set this OR verilog_files

    std::string top_module;   // empty → let yosys infer
    bool flatten   = true;    // inline sub-modules
    bool verbose   = false;
};

class YosysError : public std::runtime_error {
public:
    explicit YosysError(const std::string& msg) : std::runtime_error(msg) {}
};

// Run yosys with proc; clk2fflogic; opt; write_smt2 -wires
// Returns the SMT2 text and captured yosys log.
// Throws YosysError on non-zero exit.
YosysResult compile_to_smt2(const YosysConfig& cfg);

} // namespace symbfuzz
