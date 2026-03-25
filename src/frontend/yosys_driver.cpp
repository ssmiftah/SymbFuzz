#include "frontend/yosys_driver.h"
#include <array>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <unistd.h>
#include <fstream>
#include <sstream>
#include <stdexcept>

namespace symbfuzz {

namespace {

// Read entire file into a string
std::string read_file(const std::string& path) {
    std::ifstream f(path);
    if (!f) throw YosysError("Cannot open file: " + path);
    return {std::istreambuf_iterator<char>(f), {}};
}

// Run a shell command, capture combined stdout+stderr.
// Returns {exit_code, output}.
std::pair<int, std::string> run_command(const std::string& cmd) {
    std::string result;
    std::array<char, 4096> buf{};
    // Redirect stderr to stdout so we capture everything
    FILE* pipe = popen((cmd + " 2>&1").c_str(), "r");
    if (!pipe) throw YosysError("popen failed for command: " + cmd);
    while (fgets(buf.data(), buf.size(), pipe)) result += buf.data();
    int rc = pclose(pipe);
    return {WEXITSTATUS(rc), result};
}

} // namespace

YosysResult compile_to_smt2(const YosysConfig& cfg) {
    namespace fs = std::filesystem;

    // Resolve the list of source files (support both legacy and new field)
    std::vector<std::string> files = cfg.verilog_files;
    if (files.empty() && !cfg.verilog_path.empty())
        files.push_back(cfg.verilog_path);
    if (files.empty())
        throw YosysError("No Verilog source files specified.");
    for (const auto& f : files)
        if (!fs::exists(f))
            throw YosysError("Verilog file not found: " + f);

    // Temp directory for intermediate files
    fs::path tmp_dir = fs::temp_directory_path() / ("symbfuzz_" + std::to_string(getpid()));
    fs::create_directories(tmp_dir);
    fs::path smt2_path = tmp_dir / "design.smt2";

    // Build yosys script — read all source files then process
    std::ostringstream script;
    for (const auto& f : files)
        script << "read_verilog -sv " << f << "; ";
    if (!cfg.top_module.empty())
        script << "hierarchy -check -top " << cfg.top_module << "; ";
    else
        script << "hierarchy -check; ";
    if (cfg.flatten)
        script << "flatten; ";
    script << "proc; ";
    script << "clk2fflogic; ";
    script << "opt; ";
    script << "write_smt2 -wires " << smt2_path.string();

    // Build full yosys command
    std::string cmd = "yosys -p '" + script.str() + "'";

    if (cfg.verbose)
        cmd += " -v 2";

    auto [rc, log] = run_command(cmd);

    // Cleanup temp dir on scope exit regardless
    struct Cleanup {
        fs::path dir;
        ~Cleanup() { fs::remove_all(dir); }
    } cleanup{tmp_dir};

    if (rc != 0)
        throw YosysError("Yosys failed (exit " + std::to_string(rc) + "):\n" + log);

    if (!fs::exists(smt2_path))
        throw YosysError("Yosys succeeded but SMT2 file not produced.\n" + log);

    return YosysResult{read_file(smt2_path.string()), log};
}

} // namespace symbfuzz
