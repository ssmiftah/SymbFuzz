#include "frontend/yosys_driver.h"
#include "frontend/smt2_parser.h"
#include "bmc/target_spec.h"
#include "bmc/bmc_engine.h"
#include "bmc/result_parser.h"

#include <iostream>
#include <string>
#include <vector>
#include <cstdlib>
#include <stdexcept>

namespace {

void print_usage(const char* argv0) {
    std::cerr <<
        "Usage: " << argv0 << " [OPTIONS] <file1.v> [<file2.v> ...]\n"
        "\n"
        "  Multiple source files are read in order (dependencies before the top).\n"
        "\n"
        "Options:\n"
        "  --top <module>         Top-level module name (default: auto)\n"
        "  --target <wire>=<val>      Target constraint (full-width equality, repeatable)\n"
        "  --target-bit <wire>=<bit>=<val>  Bit-level constraint: assert bit <bit> of\n"
        "                                   <wire> equals <val> (0 or 1). Repeatable.\n"
        "  --target-flip <wire>=<bit>=<val> Temporal-flip constraint: at the final\n"
        "                                   step bit <bit> equals <val>, AND at the\n"
        "                                   preceding step it equals the opposite.\n"
        "                                   Forces a real toggle (min depth 2).\n"
        "  --max-steps <N>        BMC depth bound (default: 20)\n"
        "  --timeout <ms>         Z3 timeout per call in ms (default: 30000)\n"
        "  --no-flatten           Do not flatten module hierarchy\n"
        "  --no-zero-init         Do not force registers to 0 at step 0\n"
        "  --output json          Output format: table (default) or json\n"
        "  --verbose              Show Yosys log and BMC progress\n"
        "  --help                 Show this help\n"
        "\n"
        "Example:\n"
        "  symbfuzz sub.v top.v --top top --target count=10 --max-steps 15\n";
}

// Parse "wire=value" into a WireConstraint (width filled later)
symbfuzz::WireConstraint parse_constraint(const std::string& s) {
    auto eq = s.find('=');
    if (eq == std::string::npos)
        throw std::invalid_argument("Expected wire=value, got: " + s);
    std::string name  = s.substr(0, eq);
    uint64_t    value = std::stoull(s.substr(eq + 1));
    return {name, value, 1};  // width filled after model is parsed
}

// Parse "wire=bit=val" and set flip=true on the returned constraint.
symbfuzz::WireConstraint parse_flip_constraint(const std::string& s);

// Parse "wire=bit=val" where bit is the 0-indexed bit position and val ∈ {0,1}.
symbfuzz::WireConstraint parse_bit_constraint(const std::string& s) {
    auto eq1 = s.find('=');
    if (eq1 == std::string::npos)
        throw std::invalid_argument("Expected wire=bit=val, got: " + s);
    auto eq2 = s.find('=', eq1 + 1);
    if (eq2 == std::string::npos)
        throw std::invalid_argument("Expected wire=bit=val (missing second '='): " + s);
    std::string name = s.substr(0, eq1);
    int bit = std::stoi(s.substr(eq1 + 1, eq2 - eq1 - 1));
    uint64_t val = std::stoull(s.substr(eq2 + 1));
    if (val > 1)
        throw std::invalid_argument("Bit value must be 0 or 1: " + s);
    if (bit < 0)
        throw std::invalid_argument("Bit index must be non-negative: " + s);
    symbfuzz::WireConstraint c{name, val, 1};
    c.bit = bit;
    return c;
}

symbfuzz::WireConstraint parse_flip_constraint(const std::string& s) {
    auto c = parse_bit_constraint(s);
    c.flip = true;
    return c;
}

} // anonymous namespace

int main(int argc, char* argv[]) {
    if (argc < 2) { print_usage(argv[0]); return 1; }

    symbfuzz::YosysConfig  yosys_cfg;
    symbfuzz::TargetSpec   target;
    symbfuzz::BmcConfig    bmc_cfg;
    std::string            output_format = "table";
    bool                   show_help     = false;

    std::vector<std::string> raw_targets;
    std::vector<std::string> raw_bit_targets;
    std::vector<std::string> raw_flip_targets;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--help" || arg == "-h") {
            show_help = true;
        } else if (arg == "--top" && i + 1 < argc) {
            yosys_cfg.top_module = argv[++i];
        } else if (arg == "--target" && i + 1 < argc) {
            raw_targets.push_back(argv[++i]);
        } else if (arg == "--target-bit" && i + 1 < argc) {
            raw_bit_targets.push_back(argv[++i]);
        } else if (arg == "--target-flip" && i + 1 < argc) {
            raw_flip_targets.push_back(argv[++i]);
        } else if (arg == "--max-steps" && i + 1 < argc) {
            target.max_steps = std::stoi(argv[++i]);
        } else if (arg == "--timeout" && i + 1 < argc) {
            target.timeout_ms = std::stoi(argv[++i]);
        } else if (arg == "--no-flatten") {
            yosys_cfg.flatten = false;
        } else if (arg == "--no-zero-init") {
            target.assume_zero_init = false;
        } else if (arg == "--output" && i + 1 < argc) {
            output_format = argv[++i];
        } else if (arg == "--verbose") {
            yosys_cfg.verbose = true;
            bmc_cfg.verbose   = true;
        } else if (arg[0] != '-') {
            yosys_cfg.verilog_files.push_back(arg);
        } else {
            std::cerr << "Unknown option: " << arg << "\n";
            return 1;
        }
    }

    if (show_help) { print_usage(argv[0]); return 0; }

    if (yosys_cfg.verilog_files.empty()) {
        std::cerr << "Error: no Verilog file specified.\n";
        print_usage(argv[0]);
        return 1;
    }

    try {
        // ---- Step 1: Compile to SMT2 via Yosys -------------------------
        std::cerr << "[symbfuzz] Compiling " << yosys_cfg.verilog_files.size()
                  << " source file(s) ...\n";
        auto yosys_res = symbfuzz::compile_to_smt2(yosys_cfg);
        if (yosys_cfg.verbose) std::cerr << yosys_res.yosys_log;

        // ---- Step 2: Parse design model --------------------------------
        auto model = symbfuzz::parse_design_model(yosys_res.smt2_text);
        std::cerr << "[symbfuzz] Module: " << model.module_name << "\n";
        std::cerr << "[symbfuzz] Inputs:    " << model.inputs.size()    << "\n";
        std::cerr << "[symbfuzz] Outputs:   " << model.outputs.size()   << "\n";
        std::cerr << "[symbfuzz] Registers: " << model.registers.size() << "\n";

        // ---- Step 3: Resolve target constraints ------------------------
        auto resolve_wire = [&](symbfuzz::WireConstraint& c, const std::string& original) {
            // 1. SampleData registers (current registered state — correct semantic)
            for (const auto& r : model.registers) {
                if (r.kind == symbfuzz::RegKind::SampleData &&
                    r.arch_name == c.wire_name) {
                    c.wire_name    = r.name;
                    c.width        = r.width;
                    c.returns_bool = r.returns_bool;
                    return;
                }
            }
            // 2. Input ports
            for (const auto& p : model.inputs)
                if (p.name == c.wire_name) { c.width = p.width; return; }
            // 3. Output wires (next-state combinational values)
            for (const auto& p : model.outputs)
                if (p.name == c.wire_name) { c.width = p.width; return; }
            // 4. Internal wires
            for (const auto& w : model.wires)
                if (w.name == c.wire_name) { c.width = w.width; return; }
            throw std::runtime_error("Target wire not found in design: " + original);
        };

        if (raw_targets.empty() && raw_bit_targets.empty() && raw_flip_targets.empty()) {
            std::cerr << "Warning: no --target specified. "
                         "Checking satisfiability without a goal (trivially sat at k=1).\n";
            target.constraints.push_back({"", 0, 0}); // placeholder
        } else {
            for (const auto& rt : raw_targets) {
                auto c = parse_constraint(rt);
                c.width = 0;
                resolve_wire(c, rt);
                target.constraints.push_back(c);
            }
            for (const auto& rt : raw_bit_targets) {
                auto c = parse_bit_constraint(rt);
                int requested_bit = c.bit;
                c.width = 0;
                resolve_wire(c, rt);
                if (requested_bit >= c.width)
                    throw std::runtime_error(
                        "Bit index " + std::to_string(requested_bit) +
                        " out of range for " + c.wire_name +
                        " (width " + std::to_string(c.width) + ")");
                c.bit = requested_bit;
                target.constraints.push_back(c);
            }
            for (const auto& rt : raw_flip_targets) {
                auto c = parse_flip_constraint(rt);
                int requested_bit = c.bit;
                c.width = 0;
                resolve_wire(c, rt);
                if (requested_bit >= c.width)
                    throw std::runtime_error(
                        "Bit index " + std::to_string(requested_bit) +
                        " out of range for " + c.wire_name +
                        " (width " + std::to_string(c.width) + ")");
                c.bit  = requested_bit;
                c.flip = true;
                target.constraints.push_back(c);
            }
        }

        // ---- Step 4: Run BMC -------------------------------------------
        std::cerr << "[symbfuzz] Running BMC (max_steps=" << target.max_steps
                  << ", timeout=" << target.timeout_ms << "ms) ...\n";

        auto result = symbfuzz::run_bmc(model, target, bmc_cfg);

        // ---- Step 5: Output --------------------------------------------
        if (!result) {
            std::cerr << "[symbfuzz] No satisfying input sequence found within "
                      << target.max_steps << " steps.\n";
            return 2;
        }

        std::cerr << "[symbfuzz] Found sequence of length " << result->depth << ".\n\n";

        if (output_format == "json") {
            std::cout << symbfuzz::to_json(*result);
        } else {
            symbfuzz::print_input_sequence(*result, model);
        }

    } catch (const symbfuzz::YosysError& e) {
        std::cerr << "[Error] Yosys: " << e.what() << "\n";
        return 1;
    } catch (const std::exception& e) {
        std::cerr << "[Error] " << e.what() << "\n";
        return 1;
    }

    return 0;
}
