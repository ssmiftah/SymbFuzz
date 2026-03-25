#include "bmc/bmc_engine.h"
#include "bmc/smt2_builder.h"
#include "bmc/result_parser.h"
#include "solver/z3_solver.h"
#include <iostream>

namespace symbfuzz {

std::optional<InputSequence> run_bmc(const DesignModel& model,
                                     const TargetSpec&  target,
                                     const BmcConfig&   cfg) {
    for (int k = target.min_steps; k <= target.max_steps; ++k) {
        if (cfg.verbose)
            std::cerr << "[BMC] Trying depth k=" << k << " ...\n";

        std::string query = build_bmc_query(model, target, k);

        SolveResult res = z3_solve(query, target.timeout_ms);

        if (cfg.verbose)
            std::cerr << "[BMC] k=" << k
                      << " status=" << (res.status == SolveStatus::Sat    ? "sat"
                                      : res.status == SolveStatus::Unsat  ? "unsat"
                                                                           : "unknown")
                      << " time=" << res.elapsed_sec << "s\n";

        if (res.status == SolveStatus::Sat) {
            return parse_input_sequence(res.model_text, model, k);
        } else if (res.status == SolveStatus::Unknown) {
            std::cerr << "[BMC] Solver returned 'unknown' at depth " << k
                      << " (timeout or resource limit). Stopping.\n";
            return std::nullopt;
        }
        // unsat → no path of exactly k steps, try k+1
    }

    if (cfg.verbose)
        std::cerr << "[BMC] No path found within " << target.max_steps << " steps.\n";

    return std::nullopt;
}

} // namespace symbfuzz
