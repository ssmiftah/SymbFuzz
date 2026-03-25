#pragma once
#include <string>
#include <vector>
#include <cstdint>

namespace symbfuzz {

struct WireConstraint {
    std::string wire_name;   // must match a name in DesignModel
    uint64_t    value;       // integer value to reach
    int         width;       // bit width (filled in from DesignModel)
    bool        returns_bool = false; // true when SMT2 accessor returns Bool (not BitVec)
};

struct TargetSpec {
    std::vector<WireConstraint> constraints;
    int  min_steps  = 1;
    int  max_steps  = 20;
    int  timeout_ms = 30000;  // per Z3 call
    bool assume_zero_init = true;  // initialise all regs to 0 at s0
};

} // namespace symbfuzz
