#pragma once
#include <string>

namespace symbfuzz {

enum class SolveStatus { Sat, Unsat, Unknown };

struct SolveResult {
    SolveStatus status;
    std::string model_text;   // raw output after "sat" line (get-value response)
    double      elapsed_sec;
};

// Send smt2_text to Z3 via the C++ API.
// timeout_ms=0 means no timeout.
SolveResult z3_solve(const std::string& smt2_text, int timeout_ms = 30000);

} // namespace symbfuzz
