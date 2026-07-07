#include "utils/bitvec_utils.h"
#include <bitset>
#include <sstream>
#include <stdexcept>
#include <iomanip>
#include <algorithm>
#include <cctype>

namespace symbfuzz {

namespace {

// Normalize an input value string (from CLI, JSON, or SMT witness) into
// a stream of hex nibbles (LSB-last, MSB-first). Accepts:
//   "0xdeadbeef" / "0Xdeadbeef" / "deadbeef" — hex
//   "#xdeadbeef"                             — SMT2 hex literal
//   "#b1010..."                              — SMT2 binary literal
//   "true" / "false"                         — SMT2 Bool
//   "42" / "0"                               — decimal (only when
//                                              unambiguous — no hex letters)
// Zero-pads or truncates to `width` bits. Excess (more than width)
// upper bits are silently masked; missing upper bits are zero-filled.
std::string to_hex_nibbles(const std::string& s_in, int width) {
    std::string s = s_in;
    // Strip whitespace
    while (!s.empty() && std::isspace(static_cast<unsigned char>(s.front()))) s.erase(s.begin());
    while (!s.empty() && std::isspace(static_cast<unsigned char>(s.back()))) s.pop_back();
    if (s.empty()) s = "0";

    if (s == "true")  s = "1";
    else if (s == "false") s = "0";

    std::string binary;  // most-significant-first bit string

    // SMT2 literals
    if (s.size() >= 2 && s[0] == '#') {
        if (s[1] == 'b' || s[1] == 'B') {
            binary = s.substr(2);
        } else if (s[1] == 'x' || s[1] == 'X') {
            // hex: convert to binary
            binary.reserve(s.size() * 4);
            for (size_t i = 2; i < s.size(); ++i) {
                char c = s[i];
                int v = 0;
                if (c >= '0' && c <= '9') v = c - '0';
                else if (c >= 'a' && c <= 'f') v = 10 + (c - 'a');
                else if (c >= 'A' && c <= 'F') v = 10 + (c - 'A');
                else throw std::invalid_argument("bad hex nibble in " + s_in);
                for (int b = 3; b >= 0; --b) binary += ((v >> b) & 1) ? '1' : '0';
            }
        } else {
            throw std::invalid_argument("unknown SMT2 literal: " + s_in);
        }
    } else if (s.size() >= 2 && (s[0] == '0') && (s[1] == 'x' || s[1] == 'X')) {
        binary.reserve((s.size() - 2) * 4);
        for (size_t i = 2; i < s.size(); ++i) {
            char c = s[i];
            int v = 0;
            if (c >= '0' && c <= '9') v = c - '0';
            else if (c >= 'a' && c <= 'f') v = 10 + (c - 'a');
            else if (c >= 'A' && c <= 'F') v = 10 + (c - 'A');
            else throw std::invalid_argument("bad hex nibble in " + s_in);
            for (int b = 3; b >= 0; --b) binary += ((v >> b) & 1) ? '1' : '0';
        }
    } else {
        // Try to interpret as hex-with-letters first (any a-f/A-F), else
        // fall back to decimal. Decimal only works up to uint64_t; hex
        // is unbounded.
        bool has_hex_letter = false;
        for (char c : s) {
            if ((c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')) {
                has_hex_letter = true; break;
            }
        }
        if (has_hex_letter) {
            binary.reserve(s.size() * 4);
            for (char c : s) {
                int v = 0;
                if (c >= '0' && c <= '9') v = c - '0';
                else if (c >= 'a' && c <= 'f') v = 10 + (c - 'a');
                else if (c >= 'A' && c <= 'F') v = 10 + (c - 'A');
                else throw std::invalid_argument("bad hex nibble in " + s_in);
                for (int b = 3; b >= 0; --b) binary += ((v >> b) & 1) ? '1' : '0';
            }
        } else {
            // Decimal — bounded to uint64_t here; wide values must
            // arrive via hex form.
            uint64_t v = std::stoull(s);
            binary.reserve(64);
            for (int b = 63; b >= 0; --b) binary += ((v >> b) & 1) ? '1' : '0';
        }
    }

    // Zero-pad or truncate binary to exactly `width` bits, MSB-first.
    if ((int)binary.size() < width) {
        binary = std::string(width - binary.size(), '0') + binary;
    } else if ((int)binary.size() > width) {
        binary = binary.substr(binary.size() - width);
    }

    // Convert MSB-first binary → MSB-first hex nibbles.
    int hex_digits = (width + 3) / 4;
    int pad_bits = hex_digits * 4 - width;
    if (pad_bits > 0) binary = std::string(pad_bits, '0') + binary;
    std::string out;
    out.reserve(hex_digits);
    for (int i = 0; i < hex_digits; ++i) {
        int v = 0;
        for (int b = 0; b < 4; ++b)
            v = (v << 1) | (binary[i * 4 + b] == '1' ? 1 : 0);
        out.push_back(v < 10 ? char('0' + v) : char('a' + v - 10));
    }
    return out;
}

} // namespace

std::string to_bv_literal(uint64_t value, int width) {
    // Emit #bXXX (binary) — always unambiguous in SMT2
    std::string bits;
    bits.reserve(width);
    for (int i = width - 1; i >= 0; --i)
        bits += ((value >> i) & 1) ? '1' : '0';
    return "#b" + bits;
}

std::string to_bv_literal_from_hex(const std::string& hex_or_dec, int width) {
    // Produce a #b literal exactly `width` bits wide from any input form.
    std::string hex = to_hex_nibbles(hex_or_dec, width);
    // Convert MSB-first hex nibbles → MSB-first binary
    std::string binary;
    binary.reserve(hex.size() * 4);
    for (char c : hex) {
        int v = 0;
        if (c >= '0' && c <= '9') v = c - '0';
        else if (c >= 'a' && c <= 'f') v = 10 + (c - 'a');
        else if (c >= 'A' && c <= 'F') v = 10 + (c - 'A');
        for (int b = 3; b >= 0; --b) binary += ((v >> b) & 1) ? '1' : '0';
    }
    // Trim leading zero bits from the hex-alignment pad so length == width.
    int pad = (int)binary.size() - width;
    if (pad > 0) binary = binary.substr(pad);
    return "#b" + binary;
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
    if (lit == "true")  return 1;
    if (lit == "false") return 0;
    // plain decimal
    return std::stoull(lit);
}

std::string parse_bv_literal_to_hex(const std::string& lit, int width) {
    // Non-lossy path: normalise any input form to MSB-first hex nibbles
    // at the requested width, then return with the "0x" prefix.
    return "0x" + to_hex_nibbles(lit, width);
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
