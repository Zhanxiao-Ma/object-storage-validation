#pragma once
#include <algorithm>
#include <nlohmann/json.hpp>
#include <exception>
#include <stdexcept>

/** Completes a file upload within the SDK process, exposing one measured operation.
 * Prepared part files are consumed through the selected SDK adapter; incomplete uploads
 * are aborted on failure, so the harness never schedules multipart protocol steps.
 */
template<class Execute>
nlohmann::json multipart_file(const nlohmann::json& q, Execute execute) {
    using json=nlohmann::json;
    if(!q.contains("part_paths") || !q.at("part_paths").is_array() || q.at("part_paths").empty() || q.at("part_paths").size()>10000)
        throw std::invalid_argument("multipart_upload requires 1..10000 prepared part paths");
    auto key=q.at("key");
    auto created=execute(json{{"op","mp_create"},{"key",key}});
    auto upload=created.at("upload_id");
    json parts=json::array();
    try {
        for(const auto& path:q.at("part_paths")) {
            int number=static_cast<int>(parts.size()+1);
            auto part=execute(json{{"op","mp_part"},{"key",key},{"upload_id",upload},{"part_number",number},{"body_path",path}});
            parts.push_back(json{{"part_number",number},{"etag",part.at("etag")}});
        }
        return execute(json{{"op","mp_complete"},{"key",key},{"upload_id",upload},{"parts",parts}});
    } catch(const std::exception& error) {
        std::string original=error.what();
        try {execute(json{{"op","mp_abort"},{"key",key},{"upload_id",upload}});}
        catch(const std::exception& cleanup) {throw std::runtime_error(original+"; abort failed: "+cleanup.what());}
        throw;
    }
}

/** Splits a logical batch delete into SDK requests while preserving per-key errors. */
template<class Execute>
nlohmann::json delete_files(const nlohmann::json& q, Execute execute) {
    using json=nlohmann::json;
    const auto& objects=q.at("objects");
    if(!objects.is_array() || objects.empty())throw std::invalid_argument("delete_files requires objects");
    json errors=json::array();
    for(size_t start=0;start<objects.size();start+=1000) {
        json batch=json::array();
        for(size_t i=start;i<std::min(start+1000,objects.size());i++)batch.push_back(objects[i]);
        auto response=execute(json{{"op","delete_many"},{"objects",batch},{"quiet",true}});
        for(const auto& error:response.value("errors",json::array()))errors.push_back(error);
    }
    return json{{"errors",errors}};
}

template<class Execute>
nlohmann::json file_operation(const nlohmann::json& q, Execute execute) {
    if(q.at("op")=="multipart_upload")return multipart_file(q,execute);
    if(q.at("op")=="delete_files")return delete_files(q,execute);
    return execute(q);
}
