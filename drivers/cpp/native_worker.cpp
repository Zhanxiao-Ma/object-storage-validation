#include "stream_io.h"
#include "object_metadata.h"
#include <nlohmann/json.hpp>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#ifdef OSV_OSS
#include <alibabacloud/oss/OssClient.h>
#include <alibabacloud/oss/client/RetryStrategy.h>
#else
#include <TosClientV2.h>
#include <utils/BaseUtils.h>
#endif

using json = nlohmann::json;
static std::string env(const char* key, const std::string& fallback="") {
    auto value=std::getenv(key); return value?value:fallback;
}
static std::string required(const char* key) {
    auto value=env(key); if(value.empty()) throw std::runtime_error(std::string("missing ")+key); return value;
}

/**
 * Keeps native SDK failures distinguishable from successful operations.
 * Only genuine not-found responses are normalized for shared semantic assertions.
 */
class NativeError : public std::runtime_error {
public:
    bool missing;
    std::string error_code;
    NativeError(bool not_found,const std::string& message,const std::string& code):std::runtime_error(message),missing(not_found),error_code(code) {}
};

static std::shared_ptr<std::iostream> body(const json& q) {
    auto stream=std::make_shared<std::fstream>(q.at("body_path").get<std::string>(),std::ios::in|std::ios::binary);
    if(!*stream) throw std::runtime_error("cannot open body_path"); return stream;
}
static int64_t length(const std::shared_ptr<std::iostream>& stream) {
    stream->seekg(0,std::ios::end); auto size=stream->tellg(); stream->seekg(0); return size;
}
static void save(const json& q,const std::shared_ptr<std::iostream>& stream) {
    if (!stream) throw std::runtime_error("cannot open response stream");
    save_response_body(*stream, q.at("output_path").get<std::string>());
}

#ifdef OSV_OSS
namespace vendor=AlibabaCloud::OSS;
/**
 * Makes native OSS retry count explicit instead of silently using SDK defaults.
 * Bounded retries use exponential backoff; retry policy remains recorded as client configuration.
 */
class RetryBudget final : public vendor::RetryStrategy {
    long maximum;
public:
    explicit RetryBudget(long count):maximum(count) {}
    bool shouldRetry(const vendor::Error&,long attempted) const override { return attempted<maximum; }
    long calcDelayTimeMs(const vendor::Error&,long attempted) const override { return 100L<<std::min(attempted,8L); }
};
template<class Outcome> static auto result(Outcome outcome) {
    if(!outcome.isSuccess()) throw NativeError(outcome.error().Code()=="NoSuchKey" || outcome.error().Code()=="NotFound",outcome.error().Code()+": "+outcome.error().Message(),outcome.error().Code());
    return outcome.result();
}
/**
 * Calls official OSS C++ SDK operations for native-versus-S3 comparisons.
 * The shared JSONL contract leaves wire format and signing entirely to the SDK.
 */
class NativeWorker {
    std::string bucket=required("OSV_BUCKET");
    std::unique_ptr<vendor::OssClient> client;
public:
    NativeWorker() {
        vendor::ClientConfiguration configuration; configuration.isCname=env("OSV_CNAME","false")=="true";
        configuration.maxConnections=std::stoul(env("OSV_CONNECTIONS","16"));
        configuration.retryStrategy=std::make_shared<RetryBudget>(std::stol(env("OSV_RETRIES","0")));
        client=std::make_unique<vendor::OssClient>(required("OSV_ENDPOINT"),required("OSV_ACCESS_KEY"),required("OSV_SECRET_KEY"),env("OSV_SESSION_TOKEN"),configuration);
    }
    json execute(const json& q) {
        std::string op=q.at("op"),key=q.value("key","");
        if(op=="info") return {{"backend","oss"},{"api","native"},{"language","cpp"},{"sdk_version",OSV_NATIVE_VERSION}};
        if(op=="copy" || op=="copy_replace") {
            vendor::ObjectMetaData metadata;
            if(op=="copy_replace") {
                metadata.setContentType(q.at("content_type").get<std::string>());
                for(auto it=q.at("metadata").begin();it!=q.at("metadata").end();++it) metadata.addUserHeader(it.key(),it.value().get<std::string>());
            }
            vendor::CopyObjectRequest request(bucket,key,metadata);
            request.setCopySource(q.value("source_bucket",bucket),q.at("source_key").get<std::string>());
            if(op=="copy_replace") request.setMetadataDirective(vendor::CopyActionList::Replace);
            return {{"etag",result(client->CopyObject(request)).ETag()}};
        }
        if(op=="bucket_create") { if(!client->DoesBucketExist(bucket)) result(client->CreateBucket(bucket)); }
        else if(op=="list_page") {
            vendor::ListObjectsRequest request(bucket); request.setPrefix(q.value("prefix","")); request.setMaxKeys(q.value("max_keys",1000));
            if(q.contains("marker") || q.contains("token")) request.setMarker(q.value("token",q.value("marker","")));
            if(q.contains("delimiter")) request.setDelimiter(q.at("delimiter").get<std::string>());
            if(q.contains("encoding_type")) request.setEncodingType(q.at("encoding_type").get<std::string>());
            auto page=result(client->ListObjects(request)); json keys=json::array(),objects=json::array();
            for(const auto& object:page.ObjectSummarys()) { keys.push_back(object.Key()); objects.push_back({{"key",object.Key()},{"size",object.Size()},{"last_modified_ms",timestamp_ms(object.LastModified())},{"etag",object.ETag()}}); }
            return {{"keys",keys},{"objects",objects},{"common_prefixes",page.CommonPrefixes()},{"truncated",page.IsTruncated()},{"next_token",page.NextMarker()},{"prefix",page.Prefix()},{"delimiter",page.Delimiter()},{"max_keys",page.MaxKeys()}};
        }
        else if((op=="put" || op=="put_metadata")) {
            vendor::PutObjectRequest request(bucket,key,body(q));
            if(op=="put_metadata") {
                request.MetaData().setContentType(q.at("content_type").get<std::string>());
                for(auto it=q.at("metadata").begin();it!=q.at("metadata").end();++it) request.MetaData().addUserHeader(it.key(),it.value().get<std::string>());
            }
            result(client->PutObject(request));
        }
        else if((op=="get" || op=="get_conditional" || op=="get_metadata")) {
            vendor::GetObjectRequest request(bucket,key);
            if(q.contains("if_match")) request.setMatchingETagConstraints({q.at("if_match").get<std::string>()});
            if(q.contains("if_none_match")) request.setNonmatchingETagConstraints({q.at("if_none_match").get<std::string>()});
            if(q.contains("if_modified_since_ms")) request.setModifiedSinceConstraint(http_date(q.at("if_modified_since_ms").get<int64_t>()));
            if(q.contains("if_unmodified_since_ms")) request.setUnmodifiedSinceConstraint(http_date(q.at("if_unmodified_since_ms").get<int64_t>()));
            if(q.contains("offset")) request.setRange(q.at("offset").get<int64_t>(),q.at("offset").get<int64_t>()+q.at("length").get<int64_t>()-1);
            auto object=result(client->GetObject(request)); save(q,object.Content()); const auto& meta=object.Metadata();
            return {{"key",key},{"size",meta.ContentLength()},{"last_modified_ms",timestamp_ms(meta.LastModified())},{"etag",meta.ETag()},{"content_type",meta.ContentType()},{"metadata",meta.UserMetaData()}};
        } else if((op=="head" || op=="head_metadata")) { auto object=result(client->HeadObject(bucket,key)); return {{"key",key},{"size",object.ContentLength()},{"last_modified_ms",timestamp_ms(object.LastModified())},{"etag",object.ETag()},{"content_type",object.ContentType()},{"metadata",object.UserMetaData()}}; }
        else if(op=="delete") { result(client->DeleteObject(bucket,key)); }
        else if(op=="list") {
            json keys=json::array(); std::string marker; vendor::ListObjectsRequest request(bucket); request.setPrefix(q.at("prefix").get<std::string>());
            for(;;) {
                auto page=result(client->ListObjects(request)); for(const auto& object:page.ObjectSummarys()) keys.push_back(object.Key());
                if(!page.IsTruncated()) break;
                auto next=page.NextMarker(); if(next.empty() || next==marker) throw std::runtime_error("invalid pagination marker"); request.setMarker(next); marker=next;
            } return {{"keys",keys}};
        } else if(op=="mp_create") { return {{"upload_id",result(client->InitiateMultipartUpload(vendor::InitiateMultipartUploadRequest(bucket,key))).UploadId()}}; }
        else if(op=="mp_part") {
            auto stream=body(q); vendor::UploadPartRequest request(bucket,key,stream);
            request.setUploadId(q.at("upload_id")); request.setPartNumber(q.at("part_number")); request.setContentLength(length(stream));
            return {{"etag",result(client->UploadPart(request)).ETag()}};
        } else if(op=="mp_complete") {
            vendor::PartList parts;
            for(const auto& part:q.at("parts")) parts.emplace_back(part.at("part_number").get<int>(),part.at("etag").get<std::string>());
            result(client->CompleteMultipartUpload(vendor::CompleteMultipartUploadRequest(bucket,key,parts,q.at("upload_id").get<std::string>())));
        } else if(op=="mp_abort") { result(client->AbortMultipartUpload(vendor::AbortMultipartUploadRequest(bucket,key,q.at("upload_id")))); }
        else throw std::runtime_error("unsupported operation: "+op);
        return json::object();
    }
};
#else
namespace vendor=VolcengineTos;
template<class Outcome> static auto result(Outcome outcome) {
    if(!outcome.isSuccess()) throw NativeError(outcome.error().getStatusCode()==404,outcome.error().getMessage(),outcome.error().getCode()); return outcome.result();
}
/**
 * Uses TOS C++ SDK native endpoints to measure the same logical workload as S3.
 * Complete response consumption and full listing pagination preserve operation semantics.
 */
class NativeWorker {
    std::string bucket=required("OSV_BUCKET");
    std::unique_ptr<vendor::TosClientV2> client;
public:
    NativeWorker() {
        vendor::ClientConfig configuration; configuration.isCustomDomain=env("OSV_CNAME","false")=="true"; configuration.endPoint=required("OSV_ENDPOINT");
        auto separator=configuration.endPoint.find("://");
        auto authority=separator==std::string::npos?configuration.endPoint:configuration.endPoint.substr(separator+3);
        // This SDK rejects IPs or explicit ports before transport and later dereferences an empty response.
        // Validate the SDK constraint before the first operation so setup fails cleanly instead of crashing.
        if(!vendor::NetUtils::isNotIP(authority)) throw std::runtime_error("TOS native C++ SDK requires a DNS endpoint without explicit port");
        if(vendor::NetUtils::isS3Endpoint(authority)) throw std::runtime_error("TOS native C++ SDK requires the native endpoint, not the S3 endpoint");
        configuration.maxConnections=std::stoi(env("OSV_CONNECTIONS","16")); configuration.maxRetryCount=std::stoi(env("OSV_RETRIES","0"));
        client=std::make_unique<vendor::TosClientV2>(required("OSV_REGION"),required("OSV_ACCESS_KEY"),required("OSV_SECRET_KEY"),env("OSV_SESSION_TOKEN"),configuration);
    }
    json execute(const json& q) {
        std::string op=q.at("op"),key=q.value("key","");
        if(op=="info") return {{"backend","tos"},{"api","native"},{"language","cpp"},{"sdk_version",OSV_NATIVE_VERSION}};
        if(op=="copy" || op=="copy_replace") {
            vendor::CopyObjectV2Input request(bucket,key,q.value("source_bucket",bucket),q.at("source_key").get<std::string>());
            if(op=="copy_replace") {
                request.setMetadataDirective(vendor::MetadataDirectiveType::REPLACE);
                request.setContentType(q.at("content_type").get<std::string>());
                // SDK 2.6.30 copyObjectSetOptionHeader omits this setter; use its public request-header API.
                request.setRequestHeader({{"Content-Type",q.at("content_type").get<std::string>()}});
                request.setMeta(q.at("metadata").get<std::map<std::string,std::string>>());
            }
            return {{"etag",result(client->copyObject(request)).getETag()}};
        }
        if(op=="bucket_create") {
            auto head=client->headBucket(vendor::HeadBucketV2Input(bucket));
            if(!head.isSuccess()) { if(head.error().getStatusCode()!=404) result(head); result(client->createBucket(vendor::CreateBucketV2Input(bucket))); }
        }
        else if(op=="list_page") {
            vendor::ListObjectsType2Input request; request.setBucket(bucket); request.setPrefix(q.value("prefix","")); request.setMaxKeys(q.value("max_keys",1000));
            if(q.contains("marker")) request.setStartAfter(q.at("marker").get<std::string>());
            if(q.contains("token")) request.setContinuationToken(q.at("token").get<std::string>());
            if(q.contains("delimiter")) request.setDelimiter(q.at("delimiter").get<std::string>());
            if(q.contains("encoding_type")) request.setEncodingType(q.at("encoding_type").get<std::string>());
            auto page=result(client->listObjectsType2(request)); json keys=json::array(),objects=json::array(),prefixes=json::array();
            for(const auto& object:page.getContents()) { keys.push_back(object.getKey()); objects.push_back({{"key",object.getKey()},{"size",object.getSize()},{"last_modified_ms",int64_t(object.getLastModified())*1000},{"etag",object.getETag()}}); }
            for(const auto& prefix:page.getCommonPrefixes()) prefixes.push_back(prefix.getPrefix());
            return {{"keys",keys},{"objects",objects},{"common_prefixes",prefixes},{"truncated",page.isTruncated()},{"next_token",page.getNextContinuationToken()},{"prefix",page.getPrefix()},{"delimiter",page.getDelimiter()},{"max_keys",page.getMaxKeys()}};
        }
        else if((op=="put" || op=="put_metadata")) { auto stream=body(q); vendor::PutObjectV2Input request(bucket,key,stream); request.setContentLength(length(stream)); if(op=="put_metadata") { request.setContentType(q.at("content_type").get<std::string>()); request.setMeta(q.at("metadata").get<std::map<std::string,std::string>>()); } result(client->putObject(request)); }
        else if((op=="get" || op=="get_conditional" || op=="get_metadata" || op=="get_http")) {
            vendor::GetObjectV2Input request(bucket,key);
            if(q.contains("range_header")) request.setRange(q.at("range_header").get<std::string>());
            if(q.contains("if_match")) request.setIfMatch(q.at("if_match").get<std::string>());
            if(q.contains("if_none_match")) request.setIfNoneMatch(q.at("if_none_match").get<std::string>());
            if(q.contains("if_modified_since_ms")) request.setIfModifiedSince(q.at("if_modified_since_ms").get<int64_t>()/1000);
            if(q.contains("if_unmodified_since_ms")) request.setIfUnmodifiedSince(q.at("if_unmodified_since_ms").get<int64_t>()/1000);
            // SDK 2.6.30 treats start=end=0 as unset, so use its explicit string API for all byte ranges.
            if(q.contains("offset")) request.setRange("bytes="+std::to_string(q.at("offset").get<int64_t>())+"-"+std::to_string(q.at("offset").get<int64_t>()+q.at("length").get<int64_t>()-1));
            auto object=result(client->getObject(request));
            if(object.getContent()) save(q,object.getContent());
            else {
                // TOS 2.6.30 represents a successful zero-byte response with a null stream.
                // Accept only an actual HTTP 200 with explicit zero Content-Length, and still validate output IO.
                auto response=http_observation(object.getRequestInfo().getStatusCode(),object.getRequestInfo().getHeaders());
                if(object.getContentLength()!=0 || response["status_code"]!=200 || response["http_headers"].value("content-length","")!="0")
                    throw std::runtime_error("missing response stream without a verified empty response");
                std::ofstream output(q.at("output_path").get<std::string>(),std::ios::binary|std::ios::trunc);
                if(!output) throw std::runtime_error("cannot open output_path");
                output.close();if(!output) throw std::runtime_error("failed writing response body");
            }

            json observed={{"key",key},{"size",object.getContentLength()},{"last_modified_ms",int64_t(object.getLastModified())*1000},{"etag",object.getETags()},{"content_type",object.getContentType()},{"metadata",object.getMeta()}};
            if(op=="get_http") observed.update(http_observation(object.getRequestInfo().getStatusCode(),object.getRequestInfo().getHeaders()));
            return observed;
        } else if((op=="head" || op=="head_metadata")) { auto object=result(client->headObject(vendor::HeadObjectV2Input(bucket,key))); return {{"key",key},{"size",object.getContentLength()},{"last_modified_ms",int64_t(object.getLastModified())*1000},{"etag",object.getETag()},{"content_type",object.getContentType()},{"metadata",object.getMeta()}}; }
        else if(op=="delete") { result(client->deleteObject(vendor::DeleteObjectInput(bucket,key))); }
        else if(op=="list") {
            json keys=json::array(); vendor::ListObjectsType2Input request; request.setBucket(bucket); request.setPrefix(q.at("prefix"));
            for(;;) {
                auto page=result(client->listObjectsType2(request)); for(const auto& object:page.getContents()) keys.push_back(object.getKey());
                if(!page.isTruncated()) break;
                auto next=page.getNextContinuationToken(); if(next.empty() || next==request.getContinuationToken()) throw std::runtime_error("invalid pagination token"); request.setContinuationToken(next);
            } return {{"keys",keys}};
        } else if(op=="mp_create") { return {{"upload_id",result(client->createMultipartUpload(vendor::CreateMultipartUploadInput(bucket,key))).getUploadId()}}; }
        else if(op=="mp_part") {
            auto stream=body(q); vendor::UploadPartV2Input request(bucket,key,q.at("upload_id"),length(stream),q.at("part_number"),stream);
            return {{"etag",result(client->uploadPart(request)).getETag()}};
        } else if(op=="mp_complete") {
            std::vector<vendor::UploadedPartV2> parts;
            for(const auto& part:q.at("parts")) parts.emplace_back(part.at("part_number").get<int>(),part.at("etag").get<std::string>());
            vendor::CompleteMultipartUploadV2Input request(bucket,key,q.at("upload_id"),parts); result(client->completeMultipartUpload(request));
        } else if(op=="mp_abort") { result(client->abortMultipartUpload(vendor::AbortMultipartUploadInput(bucket,key,q.at("upload_id")))); }
        else throw std::runtime_error("unsupported operation: "+op);
        return json::object();
    }
};
#endif

int main() {
    // Keep SDK cout logging (including TOS high-latency messages) off the JSONL protocol channel.
    std::ostream protocol(std::cout.rdbuf());
    std::cout.rdbuf(std::cerr.rdbuf());
#ifdef OSV_OSS
    vendor::InitializeSdk();
#else
    vendor::InitializeClient();
#endif
    int code=0;
    try {
        NativeWorker worker;
        for(std::string line;std::getline(std::cin,line);) {
            auto start=std::chrono::steady_clock::now(); json response;
            try { auto request=json::parse(line); start=std::chrono::steady_clock::now(); response=worker.execute(request);
                if(request.at("op")=="info") {
                    response["protocol_version"]=1;
                    response["capabilities"]={"put","get","head","list","delete","mp_create","mp_part","mp_complete","mp_abort","bucket_create","list_page","get_conditional","put_metadata","get_metadata","head_metadata","copy","copy_replace"};
#ifndef OSV_OSS
                    response["capabilities"].push_back("get_http");
#endif
                    response["endpoint"]=env("OSV_ENDPOINT"); response["bucket"]=env("OSV_BUCKET");
                    response["region"]=env("OSV_REGION"); response["retries"]=env("OSV_RETRIES","0");
                    response["connections_per_worker"]=env("OSV_CONNECTIONS","16");
                }
                response["ok"]=true; }
            catch(const NativeError& error) { response={{"ok",false},{"error",error.what()},{"error_kind",error.missing?"not_found":"sdk_error"},{"error_code",error.error_code}}; }
            catch(const std::exception& error) { response={{"ok",false},{"error",error.what()},{"error_kind","worker_error"}}; }
            response["elapsed_ns"]=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count();
            protocol<<response.dump()<<std::endl;
        }
    } catch(const std::exception& error) { std::cerr<<error.what()<<std::endl; code=2; }
#ifdef OSV_OSS
    vendor::ShutdownSdk();
#else
    vendor::CloseClient();
#endif
    return code;
}
