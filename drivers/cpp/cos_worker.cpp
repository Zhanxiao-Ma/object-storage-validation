#include "object_metadata.h"
#include <cos_api.h>
#include <cos_sys_config.h>
#include <Poco/URI.h>
#include <nlohmann/json.hpp>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>

using json = nlohmann::json;
namespace vendor = qcloud_cos;
static std::string env(const char* key, const std::string& fallback="") {
    const auto value=std::getenv(key); return value ? value : fallback;
}
static std::string required(const char* key) {
    auto value=env(key); if(value.empty()) throw std::runtime_error(std::string("missing ")+key); return value;
}

/**
 * Preserve native COS errors for independent compatibility checks.
 * Normalize only actual HTTP 404 responses as missing objects.
 */
class CosError : public std::runtime_error {
public:
    bool missing;
    int status;
    std::string error_code;
    explicit CosError(const vendor::CosResult& result)
        :std::runtime_error(result.GetErrorCode()+": "+result.GetErrorMsg()),missing(result.GetHttpStatus()==404),status(result.GetHttpStatus()),error_code(result.GetErrorCode()) {}
};

/**
 * Exercise Tencent's official C++ SDK through the common worker protocol.
 * Map individual SDK operations without implementing signing or simulating service responses.
 */
class CosWorker {
    std::string bucket=required("OSV_BUCKET");
    bool https;
    std::unique_ptr<vendor::CosAPI> client;
    template<class Request> void configure(Request& request) const { if(https) request.SetHttps(); }
    static void check(const vendor::CosResult& result) { if(!result.IsSucc()) throw CosError(result); }
public:
    CosWorker() {
        Poco::URI endpoint(required("OSV_ENDPOINT"));
        if(endpoint.getHost().empty() || (endpoint.getScheme()!="http" && endpoint.getScheme()!="https")
            || !endpoint.getUserInfo().empty() || !endpoint.getQuery().empty() || !endpoint.getFragment().empty()
            || (!endpoint.getPath().empty() && endpoint.getPath()!="/"))
            throw std::runtime_error("COS endpoint must be an http(s) bucket-domain URL without path or credentials");
        https=endpoint.getScheme()=="https";
        auto separator=bucket.rfind('-');
        if(separator==std::string::npos || bucket.find_first_not_of("0123456789",separator+1)!=std::string::npos
            || separator+1==bucket.size()) throw std::runtime_error("COS bucket must include its numeric APPID suffix");
        vendor::CosConfig configuration(std::stoull(bucket.substr(separator+1)),required("OSV_ACCESS_KEY"),
            required("OSV_SECRET_KEY"),required("OSV_REGION"),env("OSV_SESSION_TOKEN"));
        configuration.SetMaxRetryTimes(std::stoull(env("OSV_RETRIES","0")));
        configuration.SetDestDomain(endpoint.getAuthority()); configuration.SetDomainSameToHost(true);
        vendor::CosSysConfig::SetLogOutType(vendor::COS_LOG_NULL);
        vendor::CosSysConfig::SetConnectionPoolSize(std::stoul(env("OSV_CONNECTIONS","16")));
        vendor::CosSysConfig::SetKeepAlive(true);
        vendor::CosSysConfig::SetRetryChangeDomain(false);
        vendor::CosSysConfig::SetConnTimeoutInms(10000); vendor::CosSysConfig::SetRecvTimeoutInms(30000);
        client=std::make_unique<vendor::CosAPI>(configuration);
    }
    json execute(const json& q) {
        std::string op=q.at("op"),key=q.value("key","");
        if(op=="info") return {{"backend","cos"},{"api","native"},{"language","cpp"},
            {"sdk_version",COS_CPP_SDK_VERSON},{"protocol_version",1},
            {"capabilities",{"put","get","head","list","delete","mp_create","mp_part","mp_complete","mp_abort","bucket_create","list_page"}},
            {"endpoint",env("OSV_ENDPOINT")},{"region",env("OSV_REGION")},{"bucket",bucket},
            {"retries",env("OSV_RETRIES","0")},{"connections_per_worker",env("OSV_CONNECTIONS","16")},
            {"range_integrity","whole-object ETag MD5 check disabled for ranges; harness verifies returned bytes"},
            {"connection_limit_semantics","max idle connections per host, not a total connection cap"},
            {"measurement","operation including local payload IO; excludes JSONL serialization"}};
        if(op=="bucket_create") {
            vendor::HeadBucketReq head(bucket); configure(head); vendor::HeadBucketResp exists;
            auto status=client->HeadBucket(head,&exists);
            if(!status.IsSucc()) { if(status.GetHttpStatus()!=404) check(status); vendor::PutBucketReq request(bucket); configure(request); vendor::PutBucketResp response; check(client->PutBucket(request,&response)); }
        } else if(op=="list_page") {
            vendor::GetBucketReq request(bucket); configure(request); request.SetPrefix(q.value("prefix","")); request.SetMaxKeys(q.value("max_keys",1000));
            if(q.contains("marker") || q.contains("token")) request.SetMarker(q.value("token",q.value("marker","")));
            if(q.contains("delimiter")) request.SetDelimiter(q.at("delimiter").get<std::string>());
            if(q.contains("encoding_type")) request.SetEncodingType(q.at("encoding_type").get<std::string>());
            vendor::GetBucketResp page; check(client->GetBucket(request,&page)); json keys=json::array(),objects=json::array();
            for(const auto& object:page.GetContents()) { keys.push_back(object.m_key); objects.push_back({{"key",object.m_key},{"size",std::stoll(object.m_size)},{"last_modified_ms",timestamp_ms(object.m_last_modified)},{"etag",object.m_etag}}); }
            return {{"keys",keys},{"objects",objects},{"common_prefixes",page.GetCommonPrefixes()},{"truncated",page.IsTruncated()},{"next_token",page.GetNextMarker()},{"prefix",page.GetPrefix()},{"delimiter",page.GetDelimiter()},{"max_keys",page.GetMaxKeys()}};
        } else if(op=="put") {
            std::ifstream input(q.at("body_path").get<std::string>(),std::ios::binary);
            if(!input) throw std::runtime_error("cannot open body_path");
            vendor::PutObjectByStreamReq request(bucket,key,input); configure(request);
            vendor::PutObjectByStreamResp response; check(client->PutObject(request,&response));
        } else if(op=="get") {
            std::ofstream output(q.at("output_path").get<std::string>(),std::ios::binary|std::ios::trunc);
            if(!output) throw std::runtime_error("cannot open output_path");
            vendor::GetObjectByStreamReq request(bucket,key,output); configure(request);
            if(q.contains("offset")) {
                request.AddHeader("Range","bytes="+std::to_string(q.at("offset").get<int64_t>())+"-"
                    +std::to_string(q.at("offset").get<int64_t>()+q.at("length").get<int64_t>()-1));
                // COS ETag describes the entire object, not the requested byte range.
                // The harness independently verifies the returned range against its original bytes.
                request.SetCheckMD5(false);
            }
            vendor::GetObjectByStreamResp response; check(client->GetObject(request,&response));
            output.close(); if(!output) throw std::runtime_error("response write failed");
        } else if(op=="head") {
            vendor::HeadObjectReq request(bucket,key); configure(request);
            vendor::HeadObjectResp response; check(client->HeadObject(request,&response)); return {{"key",key},{"size",response.GetContentLength()},{"last_modified_ms",timestamp_ms(response.GetLastModified())},{"etag",response.GetEtag()}};
        } else if(op=="list") {
            json keys=json::array(); std::string marker;
            for(;;) {
                vendor::GetBucketReq request(bucket); configure(request);
                request.SetPrefix(q.at("prefix").get<std::string>()); if(!marker.empty()) request.SetMarker(marker);
                vendor::GetBucketResp response; check(client->GetBucket(request,&response));
                for(const auto& object:response.GetContents()) keys.push_back(object.m_key);
                if(!response.IsTruncated()) break;
                auto next=response.GetNextMarker();
                if(next.empty() || next==marker) throw std::runtime_error("invalid pagination marker");
                marker=next;
            }
            return {{"keys",keys}};
        } else if(op=="delete") {
            vendor::DeleteObjectReq request(bucket,key); configure(request);
            vendor::DeleteObjectResp response; check(client->DeleteObject(request,&response));
        } else if(op=="mp_create") {
            vendor::InitMultiUploadReq request(bucket,key); configure(request);
            vendor::InitMultiUploadResp response; check(client->InitMultiUpload(request,&response)); return {{"upload_id",response.GetUploadId()}};
        } else if(op=="mp_part") {
            std::ifstream input(q.at("body_path").get<std::string>(),std::ios::binary);
            if(!input) throw std::runtime_error("cannot open body_path");
            vendor::UploadPartDataReq request(bucket,key,q.at("upload_id").get<std::string>(),input); configure(request);
            request.SetPartNumber(q.at("part_number").get<uint64_t>());
            vendor::UploadPartDataResp response; check(client->UploadPartData(request,&response)); return {{"etag",response.GetEtag()}};
        } else if(op=="mp_complete") {
            vendor::CompleteMultiUploadReq request(bucket,key,q.at("upload_id").get<std::string>()); configure(request);
            for(const auto& part:q.at("parts")) request.AddPartEtagPair(part.at("part_number").get<uint64_t>(),part.at("etag").get<std::string>());
            vendor::CompleteMultiUploadResp response; check(client->CompleteMultiUpload(request,&response));
        } else if(op=="mp_abort") {
            vendor::AbortMultiUploadReq request(bucket,key,q.at("upload_id").get<std::string>()); configure(request);
            vendor::AbortMultiUploadResp response; check(client->AbortMultiUpload(request,&response));
        } else throw std::runtime_error("unsupported operation: "+op);
        return json::object();
    }
};

int main() {
    try {
        CosWorker worker;
        for(std::string line;std::getline(std::cin,line);) {
            auto start=std::chrono::steady_clock::now(); json response;
            try { response=worker.execute(json::parse(line)); response["ok"]=true; }
            catch(const CosError& error) { response={{"ok",false},{"error",error.what()},{"error_kind",error.missing?"not_found":"sdk_error"},{"status_code",error.status},{"error_code",error.error_code}}; }
            catch(const std::exception& error) { response={{"ok",false},{"error",error.what()},{"error_kind","worker_error"}}; }
            response["elapsed_ns"]=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count();
            std::cout<<response.dump()<<std::endl;
        }
    } catch(const std::exception& error) { std::cerr<<error.what()<<std::endl; return 2; }
}
