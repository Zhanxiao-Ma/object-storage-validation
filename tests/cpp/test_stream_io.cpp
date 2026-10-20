#include "stream_io.h"
#include "object_metadata.h"
#include <filesystem>
#include <iostream>
#include <iterator>
#include <sstream>

static void require(bool condition) {
    if (!condition) throw std::runtime_error("unexpected stream IO test result");
}

/** Verify actual production output for empty, binary and multi-buffer input; expect exact bytes and truncation. */
static void roundtrip(const std::filesystem::path& path, const std::string& expected) {
    { std::ofstream old(path); old << "old-content-that-must-be-truncated"; }
    std::istringstream producer(expected);
    save_response_body(producer, path.string());
    std::ifstream consumer(path, std::ios::binary);
    const std::string actual{std::istreambuf_iterator<char>(consumer), std::istreambuf_iterator<char>()};
    require(actual == expected);
}

/** Exercise real failed input/output paths; expect a thrown error rather than a false successful empty object. */
static void failures(const std::filesystem::path& root) {
    auto rejects = [](std::istream& input, const std::string& path) {
        bool failed = false;
        try { save_response_body(input, path); }
        catch (const std::runtime_error&) { failed = true; }
        require(failed);
    };
    std::istringstream broken("unreadable");
    broken.setstate(std::ios::badbit);
    rejects(broken, (root/"bad-input").string());
    std::istringstream failed("unreadable");
    failed.setstate(std::ios::failbit);
    rejects(failed, (root/"failed-input").string());
    std::istringstream unopened("payload");
    rejects(unopened, (root/"nonexistent"/"object").string());
    if (std::filesystem::exists("/dev/full")) {
        std::istringstream full("buffered-write-must-fail");
        rejects(full, "/dev/full");
    }
}

/** Run the real shared helper boundary checks; any mismatch returns a nonzero test exit code. */
int main() {
    const auto root = std::filesystem::current_path()/"stream-io-test-data";
    try {
        std::filesystem::create_directory(root);
        roundtrip(root/"empty", "");
        roundtrip(root/"binary", std::string("a\0b\xff", 4));
        roundtrip(root/"exact-buffer", std::string(65536, char(120)));
        roundtrip(root/"multiple-buffers", std::string(200000, char(121))+"independent-tail");
        failures(root);
        // Verify real metadata conversion: ISO/RFC UTC and fractional milliseconds must match independent epoch values.
        require(timestamp_ms("2023-11-14T22:13:20Z")==1700000000000LL);
        require(timestamp_ms("Tue, 14 Nov 2023 22:13:20 GMT")==1700000000000LL);
        require(timestamp_ms("2023-11-14T22:13:20.123456Z")==1700000000123LL);
        require(timestamp_ms("2023-11-14T22:13:20.1Z")==1700000000100LL);
        require(timestamp_ms("").is_null());
        // Format literal independent dates, including year 2100 and subsecond truncation, for real SDK conditions.
        require(http_date(1700000000123LL)=="Tue, 14 Nov 2023 22:13:20 GMT");
        require(http_date(0)=="Thu, 01 Jan 1970 00:00:00 GMT");
        require(http_date(4128522211000LL)=="Fri, 29 Oct 2100 19:43:31 GMT");
        bool invalid=false; try { timestamp_ms("invalid"); } catch(const std::runtime_error&) { invalid=true; }
        require(invalid);
        std::filesystem::remove_all(root);
        return 0;
    } catch (const std::exception& error) {
        std::filesystem::remove_all(root);
        std::cerr << error.what() << std::endl;
        return 1;
    }
}
