#include "utils/bitvec_utils.h"
#include <bitset>
#include <sstream>
#include <stdexcept>
#include <iomanip>

namespace symbfuzz {

std::string to_bv_literal(uint64_t value, int width) {
    // Emit #bXXX (binary) — always unambiguous in SMT2
    std::string bits;
    bits.reserve(width);
    for (int i = width - 1; i >= 0; --i)
        bits += ((value >> i) & 1) ? '1' : '0';
    return "#b" + bits;
}

uint64_t parse_bv_literal(const std::string& lit, int /*width*/) {
    if (lit.size() >= 2 && lit[0] == '#') {
        if (lit[1] == 'b') {
            uint64_t v = 0;
            for (size_t i = 2; i < lit.size(); ++i) {
                v = (v << 1) | (lit[i] == '1' ? 1u : 0u);
            }
            return v;
        } else if (lit[1] == 'x') {
            return std::stoull(lit.substr(2), nullptr, 16);
        }
    }
    // plain decimal
    return std::stoull(lit);
}

std::string format_value(uint64_t value, int width) {
    if (width == 1) return value ? "1" : "0";
    if (width <= 8) {
        // show binary
        std::string s;
        for (int i = width - 1; i >= 0; --i) s += ((value >> i) & 1) ? '1' : '0';
        return "0b" + s;
    }
    // hex
    std::ostringstream oss;
    oss << "0x" << std::hex << std::uppercase << value
        << " (" << std::dec << value << ")";
    return oss.str();
}

} // namespace symbfuzz
