#pragma once
#include <string>
#include <cstdint>

namespace symbfuzz {

// Format an integer as an SMT2 bitvector literal: #bXXX (width bits)
std::string to_bv_literal(uint64_t value, int width);

// Format a hex-string value as an SMT2 bitvector literal at the given
// width. Accepts "0xhex", "hex", "0" and returns a `#bXXX...` literal
// exactly `width` bits wide. Truncates or zero-pads as needed. Handles
// widths beyond 64 bits without loss.
std::string to_bv_literal_from_hex(const std::string& hex_or_dec, int width);

// Parse an SMT2 bitvector literal (#bXXX or #xXXX or decimal) to uint64_t.
// Values wider than 64 bits are truncated; use parse_bv_literal_hex for
// full precision.
uint64_t parse_bv_literal(const std::string& lit, int width);

// Parse an SMT2 bitvector literal (#bXXX or #xXXX or true/false or
// decimal) into a hex-string form ("0xdeadbeef"). Zero-padded to
// ceil(width/4) hex digits. Non-lossy for arbitrary widths.
std::string parse_bv_literal_to_hex(const std::string& lit, int width);

// Format value for display (binary/hex/decimal based on width)
std::string format_value(uint64_t value, int width);

} // namespace symbfuzz
