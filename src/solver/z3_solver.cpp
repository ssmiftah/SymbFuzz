#include "solver/z3_solver.h"
#include <z3++.h>
#include <chrono>
#include <sstream>
#include <stdexcept>

namespace symbfuzz {

SolveResult z3_solve(const std::string& smt2_text, int timeout_ms) {
    auto t0 = std::chrono::steady_clock::now();

    z3::context ctx;

    // Set timeout via context parameters
    if (timeout_ms > 0) {
        z3::params p(ctx);
        p.set("timeout", static_cast<unsigned>(timeout_ms));
        // Note: timeout is set per-solver below
    }

    z3::solver solver(ctx);

    if (timeout_ms > 0) {
        z3::params p(ctx);
        p.set("timeout", static_cast<unsigned>(timeout_ms));
        solver.set(p);
    }

    // Parse the SMT2 text
    // Z3 C++ API: parse_smtlib2_string returns a list of assertions
    // It also processes (check-sat) and (get-value ...) if we use the C API
    // The cleanest approach for our use-case: use the low-level C API to
    // parse and replay commands from a string.
    //
    // z3::context::parse_string parses assertions only — for a full script
    // including (check-sat) and (get-value), use Z3_eval_smtlib2_string.

    const char* result_cstr =
        Z3_eval_smtlib2_string(ctx, smt2_text.c_str());

    auto t1 = std::chrono::steady_clock::now();
    double elapsed = std::chrono::duration<double>(t1 - t0).count();

    std::string result(result_cstr ? result_cstr : "");

    // Parse status from the first line of the result.
    // Z3_eval_smtlib2_string concatenates all command outputs:
    //   "sat\n(((|mod_n en| s0) true) ...)\n"
    //   "unsat\n(error \"model is not available\")\n"   ← get-value error after unsat is expected
    //   "unknown\n..."
    SolveStatus status = SolveStatus::Unknown;
    std::string model_text;

    // Z3_eval_smtlib2_string concatenates outputs of ALL commands.
    // Error lines (e.g. from type mismatches) may precede the "sat"/"unsat" line.
    // Scan all lines to find the status line.
    {
        std::istringstream rs(result);
        std::string line;
        while (std::getline(rs, line)) {
            // strip \r
            if (!line.empty() && line.back() == '\r') line.pop_back();
            if (line == "sat")    { status = SolveStatus::Sat;   break; }
            if (line == "unsat")  { status = SolveStatus::Unsat; break; }
            if (line == "unknown"){ status = SolveStatus::Unknown; break; }
            // skip error / empty lines and keep scanning
        }
        // model_text = everything after the status line
        if (status != SolveStatus::Unknown) {
            size_t pos = result.find('\n');
            while (pos != std::string::npos) {
                std::string seg = result.substr(0, pos);
                if (!seg.empty() && seg.back() == '\r') seg.pop_back();
                if (seg == "sat" || seg == "unsat" || seg == "unknown") {
                    model_text = result.substr(pos + 1);
                    break;
                }
                pos = result.find('\n', pos + 1);
            }
        }
    }

    // Only surface Z3 errors when the result is genuinely indeterminate.
    // When unsat, the "model is not available" error on get-value is expected.
    if (status == SolveStatus::Unknown) {
        Z3_error_code err = Z3_get_error_code(ctx);
        if (err != Z3_OK) {
            std::string msg = Z3_get_error_msg(ctx, err);
            throw std::runtime_error("Z3 error: " + msg + "\nOutput: " + result);
        }
    }

    return SolveResult{status, model_text, elapsed};
}

} // namespace symbfuzz
