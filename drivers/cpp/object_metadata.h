#pragma once
#include <nlohmann/json.hpp>
#include <ctime>
#include <cctype>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <string>

/** Convert SDK timestamps to epoch milliseconds without inventing missing metadata. */
inline nlohmann::json timestamp_ms(const std::string& value) {
    if(value.empty()) return nullptr;
    const bool iso=value.size()>10 && value[10]=='T';
    std::tm parsed{};
    std::istringstream input(value);
    input.imbue(std::locale::classic());
    input >> std::get_time(&parsed,iso?"%Y-%m-%dT%H:%M:%S":"%a, %d %b %Y %H:%M:%S");
    if(input.fail()) throw std::runtime_error("invalid SDK last-modified timestamp: "+value);
    int64_t fraction=0;
    if(iso) {
        size_t position=19;
        if(position<value.size() && value[position]=='.') {
            ++position; int digits=0;
            while(position<value.size() && std::isdigit(static_cast<unsigned char>(value[position]))) {
                if(digits<3) fraction=fraction*10+(value[position]-'0');
                ++digits; ++position;
            }
            if(digits==0) throw std::runtime_error("invalid SDK timestamp fraction");
            while(digits++<3) fraction*=10;
        }
        if(value.substr(position)!="Z" && value.substr(position)!="+00:00")
            throw std::runtime_error("SDK timestamp is not UTC");
    }
    return static_cast<int64_t>(timegm(&parsed))*1000+fraction;
}

/** Format epoch milliseconds as an HTTP UTC date for SDKs accepting textual request conditions. */
inline std::string http_date(int64_t milliseconds) {
    const auto seconds=static_cast<std::time_t>(milliseconds/1000);
    std::tm utc{};
    if(!gmtime_r(&seconds,&utc)) throw std::runtime_error("condition date is out of range");
    std::ostringstream output; output.imbue(std::locale::classic());
    output << std::put_time(&utc,"%a, %d %b %Y %H:%M:%S GMT");
    return output.str();
}

/** Preserve an observed SDK HTTP status and headers; never derive them from requested ranges or payload length. */
template<class Headers> inline nlohmann::json http_observation(int status,const Headers& headers) {
    if(status<=0) throw std::runtime_error("SDK did not expose an HTTP response status");
    nlohmann::json values=nlohmann::json::object();
    for(const auto& entry:headers) {
        std::string name=entry.first.c_str();
        for(char& ch:name) ch=static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
        values[name]=std::string(entry.second.c_str());
    }
    return {{"status_code",status},{"http_headers",values}};
}
