#pragma once
#include <string>
#include <cstdint>

namespace symbfuzz {

// Format an integer as an SMT2 bitvector literal: #bXXX (width bits)
std::string to_bv_literal(uint64_t value, int width);

// Parse an SMT2 bitvector literal (#bXXX or #xXXX or decimal) to uint64_t
uint64_t parse_bv_literal(const std::string& lit, int width);

// Format value for display (binary/hex/decimal based on width)
std::string format_value(uint64_t value, int width);

} // namespace symbfuzz
