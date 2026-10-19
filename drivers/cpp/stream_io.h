#pragma once
#include <array>
#include <fstream>
#include <istream>
#include <stdexcept>
#include <string>

/**
 * Save complete SDK response bodies, including valid empty streams.
 * Explicit buffered reads avoid ostream/streambuf zero-byte failbit while still
 * detecting input errors, file-open errors and buffered output/close failures.
 */
inline void save_response_body(std::istream& input, const std::string& path) {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    if (!output) throw std::runtime_error("cannot open output_path");
    std::array<char, 64 * 1024> buffer{};
    while (input) {
        input.read(buffer.data(), buffer.size());
        if (input.gcount() > 0) output.write(buffer.data(), input.gcount());
        if (!output) throw std::runtime_error("failed writing response body");
    }
    if (input.bad() || (input.fail() && !input.eof()))
        throw std::runtime_error("failed reading response body");
    output.close();
    if (!output) throw std::runtime_error("failed writing response body");
}
