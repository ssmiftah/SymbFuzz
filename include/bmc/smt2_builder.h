#pragma once
#include "frontend/smt2_parser.h"
#include "bmc/target_spec.h"
#include <string>

namespace symbfuzz {

// Build the complete SMT2 text for a BMC query of depth `depth`.
// Embeds the design's SMT2, declares states s0..s{depth},
// chains mod_t, asserts mod_i(s0), asserts goal at s{depth},
// and appends get-value for all input ports across all steps.
std::string build_bmc_query(const DesignModel& model,
                             const TargetSpec&  target,
                             int                depth);

} // namespace symbfuzz
