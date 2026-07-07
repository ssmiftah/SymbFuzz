#pragma once
#include "frontend/smt2_parser.h"
#include <string>
#include <vector>
#include <map>
#include <cstdint>

namespace symbfuzz {

// One cycle's worth of input port values. Values are stored as
// hex-string form ("0xdeadbeef") so widths >64 bits round-trip without
// truncation. Narrow values (≤64 bits) are still hex to keep the
// witness JSON emission uniform; Python decodes both forms.
using CycleInputs = std::map<std::string, std::string>;

struct InputSequence {
    int                       depth;   // number of cycles
    std::vector<CycleInputs>  steps;   // steps[i] = inputs at cycle i
};

// Parse Z3 get-value output into an InputSequence.
// model_text is everything after "sat\n".
InputSequence parse_input_sequence(const std::string& model_text,
                                   const DesignModel& model,
                                   int depth);

// Pretty-print the input sequence as a table
void print_input_sequence(const InputSequence& seq,
                          const DesignModel&   model);

// Emit as JSON
std::string to_json(const InputSequence& seq);

} // namespace symbfuzz
