#pragma once
#include <aws/s3/S3Client.h>
#include <aws/core/http/HttpTypes.h>
#include <limits>

/**
 * Bridges Ceph presigned HTTP vectors through the official selected S3 client.
 * Preserve real identity credentials and official URL/ACL signing; reject signed
 * negative durations that the SDK uint64 API cannot represent instead of wrapping
 * them or forging server responses. No handwritten signing is performed.
 */
namespace osv_presigning {
inline json capabilities() { return json::array({"presign","presign_v4"}); }
inline std::string text(const json& q,const char* name) {
    if(!q.contains(name) || !q.at(name).is_string() || q.at(name).get<std::string>().empty())throw std::runtime_error(std::string(name)+" must be a non-empty string");
    return q.at(name).get<std::string>();
}
inline json execute(Aws::S3::S3Client& client,const json& q) {
    if(q.contains("signature_version") && (!q.at("signature_version").is_string() || q.at("signature_version")!="v4"))throw std::runtime_error("official C++ S3 client supports v4 only");
    const auto method=text(q,"method"),bucket=text(q,"bucket"),key=text(q,"key");
    if(method!="GET" && method!="PUT")throw std::runtime_error("presign method must be GET or PUT");
    if(q.contains("acl") && (!q.at("acl").is_string() || method!="PUT"))throw std::runtime_error("acl requires PUT and a string");
    Aws::Http::HeaderValueCollection headers;
    if(q.contains("acl"))headers.emplace("x-amz-acl",q.at("acl").get<std::string>().c_str());
    Aws::String url;
    auto verb=method=="GET"?Aws::Http::HttpMethod::HTTP_GET:Aws::Http::HttpMethod::HTTP_PUT;
    if(q.contains("expires_seconds")) {
        if(!q.at("expires_seconds").is_number_integer() || q.at("expires_seconds").get<long long>()<0)throw std::runtime_error("official C++ presigner cannot represent negative expires_seconds");
        url=client.GeneratePresignedUrl(bucket.c_str(),key.c_str(),verb,headers,q.at("expires_seconds").get<uint64_t>());
    } else url=client.GeneratePresignedUrl(bucket.c_str(),key.c_str(),verb,headers);
    if(url.empty())throw std::runtime_error("official C++ presigner returned an empty URL");
    json observed=json::object();for(const auto& item:headers)observed[std::string(item.first.c_str())]=json::array({std::string(item.second.c_str())});
    return {{"url",std::string(url.c_str())},{"required_headers",observed},{"signature_version","v4"},{"expires_input_present",q.contains("expires_seconds")}};
}
}
