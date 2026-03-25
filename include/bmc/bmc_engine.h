#pragma once
#include "frontend/smt2_parser.h"
#include "bmc/target_spec.h"
#include "bmc/result_parser.h"
#include <optional>

namespace symbfuzz {

struct BmcConfig {
    bool verbose = false;
};

// Run bounded model checking.
// Returns the shortest input sequence that satisfies all target constraints,
// or std::nullopt if no path exists within target.max_steps.
std::optional<InputSequence> run_bmc(const DesignModel& model,
                                     const TargetSpec&  target,
                                     const BmcConfig&   cfg = {});

} // namespace symbfuzz
