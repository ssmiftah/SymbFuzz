#include "bmc/result_parser.h"
#include "utils/bitvec_utils.h"
#include <iostream>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <regex>

namespace symbfuzz {

namespace {

// Minimal s-expression tokeniser
// Handles: ( ) atoms (including |quoted atoms|)
enum class TokKind { LParen, RParen, Atom };
struct Token { TokKind kind; std::string text; };

std::vector<Token> tokenise(const std::string& s) {
    std::vector<Token> toks;
    size_t i = 0;
    while (i < s.size()) {
        char c = s[i];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') { ++i; continue; }
        if (c == '(') { toks.push_back({TokKind::LParen, "("}); ++i; continue; }
        if (c == ')') { toks.push_back({TokKind::RParen, ")"}); ++i; continue; }
        if (c == '|') {
            // Quoted atom: |...|
            size_t j = s.find('|', i + 1);
            if (j == std::string::npos) j = s.size();
            toks.push_back({TokKind::Atom, s.substr(i, j - i + 1)});
            i = j + 1;
            continue;
        }
        // Regular atom: read until whitespace or paren
        size_t j = i;
        while (j < s.size() && s[j] != ' ' && s[j] != '\t' &&
               s[j] != '\n' && s[j] != '\r' &&
               s[j] != '(' && s[j] != ')') ++j;
        toks.push_back({TokKind::Atom, s.substr(i, j - i)});
        i = j;
    }
    return toks;
}

// Extract step index from a state variable name like "s3" → 3
int step_from_state(const std::string& state_name) {
    if (state_name.size() >= 2 && state_name[0] == 's') {
        try { return std::stoi(state_name.substr(1)); }
        catch (...) {}
    }
    return -1;
}

// Extract port name from an accessor atom like "|mymod_n clk|" → "clk"
// The accessor looks like |<module>_n <portname>|
std::string port_from_accessor(const std::string& acc, const std::string& module) {
    // acc is e.g. "|mymod_n en|"
    std::string prefix = "|" + module + "_n ";
    if (acc.size() > prefix.size() + 1 &&
        acc.compare(0, prefix.size(), prefix) == 0) {
        return acc.substr(prefix.size(), acc.size() - prefix.size() - 1);
    }
    return "";
}

} // namespace

InputSequence parse_input_sequence(const std::string& model_text,
                                   const DesignModel& model,
                                   int depth) {
    InputSequence seq;
    seq.depth = depth;
    seq.steps.resize(depth);

    // model_text is the get-value response:
    // (((|mod_n en| s0) false)
    //  ((|mod_n en| s1) true)
    //  ...)
    // Each entry is: ((accessor state) value)

    auto tokens = tokenise(model_text);
    size_t i = 0;
    size_t n = tokens.size();

    // Skip leading "(" of the outer list
    if (i < n && tokens[i].kind == TokKind::LParen) ++i;

    while (i < n && tokens[i].kind == TokKind::LParen) {
        ++i; // consume "(" of entry

        // Inner: (accessor state)
        if (i >= n || tokens[i].kind != TokKind::LParen) break;
        ++i; // consume "("

        // accessor atom
        std::string acc_atom = (i < n) ? tokens[i].text : "";
        ++i;
        // state atom
        std::string state_atom = (i < n) ? tokens[i].text : "";
        ++i;
        // consume ")"
        if (i < n && tokens[i].kind == TokKind::RParen) ++i;

        // value
        std::string value_atom = (i < n) ? tokens[i].text : "";
        ++i;

        // consume outer ")"
        if (i < n && tokens[i].kind == TokKind::RParen) ++i;

        // Map back to port and step
        std::string port_name = port_from_accessor(acc_atom, model.module_name);
        int step = step_from_state(state_atom);

        if (port_name.empty() || step < 0 || step >= depth) continue;

        // Find bit width of this port
        int width = 1;
        for (const auto& p : model.inputs) {
            if (p.name == port_name) { width = p.width; break; }
        }

        // Parse value: "true"/"false" or "#bXXX" or "#xXXX" or decimal
        uint64_t val = 0;
        if (value_atom == "true")  val = 1;
        else if (value_atom == "false") val = 0;
        else val = parse_bv_literal(value_atom, width);

        seq.steps[step][port_name] = val;
    }

    return seq;
}

void print_input_sequence(const InputSequence& seq, const DesignModel& model) {
    // Collect non-clock input names (filter out "clk")
    std::vector<std::string> ports;
    for (const auto& p : model.inputs) {
        if (p.name != "clk" && p.name != "clock") ports.push_back(p.name);
    }

    // Header
    int step_w = 5;
    std::cout << std::setw(step_w) << "Step";
    for (const auto& pn : ports) std::cout << "  " << std::setw(10) << pn;
    std::cout << "\n";

    std::cout << std::string(step_w, '-');
    for (size_t j = 0; j < ports.size(); ++j)
        std::cout << "--" << std::string(10, '-');
    std::cout << "\n";

    for (int i = 0; i < seq.depth; ++i) {
        std::cout << std::setw(step_w) << i;
        for (const auto& pn : ports) {
            uint64_t val = 0;
            auto it = seq.steps[i].find(pn);
            if (it != seq.steps[i].end()) val = it->second;

            int width = 1;
            for (const auto& p : model.inputs)
                if (p.name == pn) { width = p.width; break; }

            std::cout << "  " << std::setw(10) << format_value(val, width);
        }
        std::cout << "\n";
    }
}

std::string to_json(const InputSequence& seq) {
    std::ostringstream j;
    j << "{\n  \"depth\": " << seq.depth << ",\n  \"steps\": [\n";
    for (int i = 0; i < seq.depth; ++i) {
        j << "    {";
        bool first = true;
        for (const auto& [port, val] : seq.steps[i]) {
            if (!first) j << ", ";
            first = false;
            j << "\"" << port << "\": " << val;
        }
        j << "}";
        if (i + 1 < seq.depth) j << ",";
        j << "\n";
    }
    j << "  ]\n}\n";
    return j.str();
}

} // namespace symbfuzz
