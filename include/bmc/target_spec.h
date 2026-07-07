#pragma once
#include <string>
#include <vector>
#include <cstdint>

namespace symbfuzz {

struct WireConstraint {
    std::string wire_name;   // must match a name in DesignModel
    // Value in hex-string form ("0xdeadbeef", any width). Full-width
    // equality when bit == -1; single-bit value ("0" or "1") when
    // bit >= 0. Hex form is chosen so widths >64 bits round-trip
    // between Python, CLI, and SMT2 without any uint64 truncation.
    // Kept as a string even for narrow values because the smt2_builder
    // wants a bit-width-parameterised SMT2 literal, not a native int.
    std::string value;
    int         width;       // bit width (filled in from DesignModel)
    bool        returns_bool = false; // true when SMT2 accessor returns Bool (not BitVec)
    int         bit = -1;    // -1 → full-wire equality (legacy).
                             //  k → single-bit extract at index k (0 = LSB).
    bool        flip = false;// when true, also assert bit/wire at step k-1
                             // equals NOT(value). Forces a *transition*
                             // between steps k-1 and k, not merely final-state
                             // equality. Used by Phase 2 toggle-coverage BMC.
};

struct TargetSpec {
    std::vector<WireConstraint> constraints;
    int  min_steps  = 1;
    int  max_steps  = 20;
    int  timeout_ms = 30000;  // per Z3 call
    bool assume_zero_init = true;  // initialise all regs to 0 at s0
};

} // namespace symbfuzz
