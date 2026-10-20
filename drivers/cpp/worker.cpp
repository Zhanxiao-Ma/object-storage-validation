#include "stream_io.h"
#include "object_metadata.h"
#include "http_observer.h"
#include "list_observation.h"
#include <aws/s3/model/CreateBucketRequest.h>
#include <aws/s3/model/CopyObjectRequest.h>
#include <aws/s3/model/PutObjectTaggingRequest.h>
#include <aws/s3/model/GetObjectTaggingRequest.h>
#include <aws/s3/model/DeleteObjectTaggingRequest.h>
#include <aws/s3/model/HeadBucketRequest.h>
#include <aws/s3/model/DeleteBucketRequest.h>
#include <aws/s3/model/GetBucketLocationRequest.h>
#include <aws/core/Aws.h>
#include <aws/core/http/HttpResponse.h>
#include <aws/core/VersionConfig.h>
#include <aws/core/auth/AWSCredentials.h>
#include <aws/core/client/DefaultRetryStrategy.h>
#include <aws/s3/S3Client.h>
#include <aws/s3/model/AbortMultipartUploadRequest.h>
#include <aws/s3/model/CompleteMultipartUploadRequest.h>
#include <aws/s3/model/CreateMultipartUploadRequest.h>
#include <aws/s3/model/DeleteObjectRequest.h>
#include <aws/s3/model/GetObjectRequest.h>
#include <aws/s3/model/HeadObjectRequest.h>
#include <aws/s3/model/ListObjectsV2Request.h>
#include <aws/s3/model/ListObjectsRequest.h>
#include <aws/s3/model/PutObjectRequest.h>
#include <aws/s3/model/UploadPartRequest.h>
#include <nlohmann/json.hpp>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>

using json = nlohmann::json;

static std::string env(const char* name, const std::string& fallback = "") {
    auto value = std::getenv(name);
    return value ? value : fallback;
}

static std::string required(const char* name) {
    auto value = env(name);
    if (value.empty()) throw std::runtime_error(std::string("missing ")+name);
    return value;
}

/**
 * Preserves HTTP status information without converting SDK failures to successes.
 * The common protocol reports not_found separately for compatibility assertions.
 */
class SdkError : public std::runtime_error {
public:
    int status;
    std::string error_code;
    SdkError(int code, const std::string& message, const std::string& name) : std::runtime_error(message), status(code), error_code(name) {}
};

template<class Outcome> static void checked(const Outcome& outcome) {
    if (!outcome.IsSuccess()) {
        auto& error = outcome.GetError();
        throw SdkError(static_cast<int>(error.GetResponseCode()), error.GetMessage().c_str(), error.GetExceptionName().c_str());
    }
}

/**
 * Runs AWS C++ SDK S3 operations for direct comparison with other C++ adapters.
 * A persistent client excludes process startup; each timed operation includes payload I/O.
 */
class S3Worker {
    std::string bucket = required("OSV_BUCKET");
    std::shared_ptr<Aws::S3::S3Client> client;
public:
    S3Worker() {
        Aws::Client::ClientConfiguration configuration;
        configuration.endpointOverride = required("OSV_ENDPOINT");
        configuration.region = required("OSV_REGION");
        configuration.scheme = configuration.endpointOverride.find("http://") == 0
            ? Aws::Http::Scheme::HTTP : Aws::Http::Scheme::HTTPS;
        configuration.maxConnections = std::stoul(env("OSV_CONNECTIONS", "16"));
        configuration.requestTimeoutMs = 30000;
        configuration.connectTimeoutMs = 10000;
        configuration.retryStrategy = Aws::MakeShared<Aws::Client::DefaultRetryStrategy>(
            "osv", std::stol(env("OSV_RETRIES", "0")));
        Aws::Auth::AWSCredentials credentials(required("OSV_ACCESS_KEY"), required("OSV_SECRET_KEY"), env("OSV_SESSION_TOKEN"));
        client = std::make_shared<Aws::S3::S3Client>(credentials, configuration,
            Aws::Client::AWSAuthV4Signer::PayloadSigningPolicy::Never, env("OSV_PATH_STYLE", "false") != "true");
    }
    json execute(const json& q) {
        std::string op = q.at("op"), key = q.value("key", "");
        const std::string& bucket=q.contains("bucket")?q.at("bucket").get_ref<const std::string&>():this->bucket;
        if (op == "info") return {{"backend", "s3"}, {"api", "s3"}, {"language", "cpp"}, {"sdk", "aws-sdk-cpp"},
            {"sdk_version", AWS_SDK_VERSION_STRING}, {"measurement", "operation including local payload IO; excludes JSONL serialization"}};
        if(op=="list_v1_page" || op=="list_v2_observed") {
            json observed;HttpObservationScope observation(&observed,true);
            if(op=="list_v1_page") {
                Aws::S3::Model::ListObjectsRequest request;request.SetBucket(bucket);
                if(q.contains("prefix")) request.SetPrefix(q.at("prefix").get<std::string>());
                if(q.contains("delimiter")) request.SetDelimiter(q.at("delimiter").get<std::string>());
                if(q.contains("marker")) request.SetMarker(q.at("marker").get<std::string>());
                if(q.contains("max_keys")) request.SetMaxKeys(q.at("max_keys").get<int>());
                if(q.contains("encoding_type")) request.SetEncodingType(Aws::S3::Model::EncodingTypeMapper::GetEncodingTypeForName(q.at("encoding_type").get<std::string>()));
                auto outcome=client->ListObjects(request);checked(outcome);return observed_list_page(outcome.GetResult(),observed);
            }
            Aws::S3::Model::ListObjectsV2Request request;request.SetBucket(bucket);
            if(q.contains("prefix")) request.SetPrefix(q.at("prefix").get<std::string>());
            if(q.contains("delimiter")) request.SetDelimiter(q.at("delimiter").get<std::string>());
            if(q.contains("start_after")) request.SetStartAfter(q.at("start_after").get<std::string>());
            if(q.contains("token")) request.SetContinuationToken(q.at("token").get<std::string>());
            if(q.contains("max_keys")) request.SetMaxKeys(q.at("max_keys").get<int>());
            if(q.contains("fetch_owner")) request.SetFetchOwner(q.at("fetch_owner").get<bool>());
            if(q.contains("encoding_type")) request.SetEncodingType(Aws::S3::Model::EncodingTypeMapper::GetEncodingTypeForName(q.at("encoding_type").get<std::string>()));
            auto outcome=client->ListObjectsV2(request);checked(outcome);return observed_list_page(outcome.GetResult(),observed);
        }
        if(op=="bucket_create_raw" || op=="bucket_head" || op=="bucket_delete" || op=="bucket_location") {
            json http;HttpObservationScope observation(&http);
            if(op=="bucket_create_raw") {
                Aws::S3::Model::CreateBucketRequest request;request.SetBucket(bucket);
                if(q.contains("location")) {Aws::S3::Model::CreateBucketConfiguration configuration;configuration.SetLocationConstraint(Aws::S3::Model::BucketLocationConstraintMapper::GetBucketLocationConstraintForName(q.at("location").get<std::string>()));request.SetCreateBucketConfiguration(configuration);}
                checked(client->CreateBucket(request));
            } else if(op=="bucket_head") {
                Aws::S3::Model::HeadBucketRequest request;request.SetBucket(bucket);checked(client->HeadBucket(request));
            } else if(op=="bucket_delete") {
                Aws::S3::Model::DeleteBucketRequest request;request.SetBucket(bucket);checked(client->DeleteBucket(request));
            } else {
                Aws::S3::Model::GetBucketLocationRequest request;request.SetBucket(bucket);auto outcome=client->GetBucketLocation(request);checked(outcome);
                auto location=Aws::S3::Model::BucketLocationConstraintMapper::GetNameForBucketLocationConstraint(outcome.GetResult().GetLocationConstraint());
                http["location"]=location.empty()?json(nullptr):json(std::string(location.c_str()));
            }
            if(!http.contains("status_code")) throw std::runtime_error("SDK HTTP observation unavailable");
            return http;
        }
        if(op=="copy" || op=="copy_replace") {
            Aws::S3::Model::CopyObjectRequest request; request.SetBucket(bucket); request.SetKey(key);
            request.SetCopySource(q.value("source_bucket",bucket)+"/"+q.at("source_key").get<std::string>());
            if(op=="copy_replace") {
                request.SetMetadataDirective(Aws::S3::Model::MetadataDirective::REPLACE);
                request.SetContentType(q.at("content_type").get<std::string>());
                for(auto it=q.at("metadata").begin();it!=q.at("metadata").end();++it) request.AddMetadata(it.key(),it.value().get<std::string>());
            }
            auto outcome=client->CopyObject(request); checked(outcome);
            return {{"etag",std::string(outcome.GetResult().GetCopyObjectResultDetails().GetETag().c_str())}};
        }
        if(op=="tags_put" || op=="tags_get" || op=="tags_delete") {
            json observed; HttpObservationScope observation(&observed);
            if(op=="tags_put") {
                Aws::S3::Model::PutObjectTaggingRequest request;request.SetBucket(bucket);request.SetKey(q.at("key").get<std::string>());
                Aws::S3::Model::Tagging tagging;
                for(const auto& pair:q.at("tags")) {Aws::S3::Model::Tag tag;tag.SetKey(pair.at("key").get<std::string>());tag.SetValue(pair.at("value").get<std::string>());tagging.AddTagSet(tag);}
                request.SetTagging(tagging);checked(client->PutObjectTagging(request));
            } else if(op=="tags_get") {
                Aws::S3::Model::GetObjectTaggingRequest request;request.SetBucket(bucket);request.SetKey(q.at("key").get<std::string>());
                auto outcome=client->GetObjectTagging(request);checked(outcome);json tags=json::array();
                for(const auto& tag:outcome.GetResult().GetTagSet()) tags.push_back({{"key",std::string(tag.GetKey().c_str())},{"value",std::string(tag.GetValue().c_str())}});
                observed["tags"]=tags;
            } else {
                Aws::S3::Model::DeleteObjectTaggingRequest request;request.SetBucket(bucket);request.SetKey(q.at("key").get<std::string>());checked(client->DeleteObjectTagging(request));
            }
            if(!observed.contains("status_code")) throw std::runtime_error("SDK did not expose tag response HTTP status");
            return observed;
        }
        if (op == "bucket_create") {
            Aws::S3::Model::HeadBucketRequest head; head.SetBucket(bucket);
            auto exists=client->HeadBucket(head);
            if(!exists.IsSuccess()) {
                if(static_cast<int>(exists.GetError().GetResponseCode())!=404) checked(exists);
                Aws::S3::Model::CreateBucketRequest request; request.SetBucket(bucket);
                if(required("OSV_REGION")!="us-east-1") {
                    Aws::S3::Model::CreateBucketConfiguration config;
                    config.SetLocationConstraint(Aws::S3::Model::BucketLocationConstraintMapper::GetBucketLocationConstraintForName(required("OSV_REGION")));
                    request.SetCreateBucketConfiguration(config);
                }
                checked(client->CreateBucket(request));
            }
        } else if (op == "list_page") {
            Aws::S3::Model::ListObjectsV2Request request; request.SetBucket(bucket); request.SetPrefix(q.value("prefix", ""));
            request.SetMaxKeys(q.value("max_keys",1000));
            if(q.contains("marker")) request.SetStartAfter(q.at("marker").get<std::string>());
            if(q.contains("token")) request.SetContinuationToken(q.at("token").get<std::string>());
            if(q.contains("delimiter")) request.SetDelimiter(q.at("delimiter").get<std::string>());
            if(q.contains("encoding_type")) request.SetEncodingType(Aws::S3::Model::EncodingTypeMapper::GetEncodingTypeForName(q.at("encoding_type").get<std::string>()));
            auto outcome=client->ListObjectsV2(request); checked(outcome);
            const auto& page=outcome.GetResult(); json keys=json::array(),objects=json::array(),prefixes=json::array();
            for(const auto& object:page.GetContents()) {
                std::string name=object.GetKey().c_str(); keys.push_back(name);
                objects.push_back({{"key",name},{"size",object.GetSize()},{"last_modified_ms",object.GetLastModified().Millis()},{"etag",std::string(object.GetETag().c_str())}});
            }
            for(const auto& prefix:page.GetCommonPrefixes()) prefixes.push_back(std::string(prefix.GetPrefix().c_str()));
            return {{"keys",keys},{"objects",objects},{"common_prefixes",prefixes},{"truncated",page.GetIsTruncated()},{"next_token",std::string(page.GetNextContinuationToken().c_str())},{"prefix",std::string(page.GetPrefix().c_str())},{"delimiter",std::string(page.GetDelimiter().c_str())},{"max_keys",page.GetMaxKeys()}};
        } else if ((op == "put" || op == "put_metadata" || op == "put_http")) {
            Aws::S3::Model::PutObjectRequest request;
            request.SetBucket(bucket); request.SetKey(key);
            auto stream = Aws::MakeShared<Aws::FStream>("osv", q.at("body_path").get<std::string>(), std::ios::in | std::ios::binary);
            if (!*stream) throw std::runtime_error("cannot open body_path");
            request.SetBody(stream);
            if(op=="put_metadata") {
                request.SetContentType(q.at("content_type").get<std::string>());
                for(auto it=q.at("metadata").begin();it!=q.at("metadata").end();++it) request.AddMetadata(it.key(),it.value().get<std::string>());
            }
            if(q.contains("cache_control")) request.SetCacheControl(q.at("cache_control").get<std::string>());
            if(q.contains("expires_ms")) request.SetExpires(Aws::Utils::DateTime(q.at("expires_ms").get<int64_t>()));
            json http;HttpObservationScope observation(op=="put_http"?&http:nullptr);
            auto outcome=client->PutObject(request);checked(outcome);
            if(op=="put_http") {if(!http.contains("status_code")) throw std::runtime_error("SDK HTTP observation unavailable");http["etag"]=std::string(outcome.GetResult().GetETag().c_str());return http;}

        } else if ((op == "get" || op == "get_conditional" || op == "get_metadata" || op == "get_http")) {
            Aws::S3::Model::GetObjectRequest request;
            request.SetBucket(bucket); request.SetKey(key);
            json http=json::object();
            HttpObservationScope observation(op=="get_http"?&http:nullptr);
            if(q.contains("if_match")) request.SetIfMatch(q.at("if_match").get<std::string>());
            if(q.contains("if_none_match")) request.SetIfNoneMatch(q.at("if_none_match").get<std::string>());
            if(q.contains("if_modified_since_ms")) request.SetIfModifiedSince(Aws::Utils::DateTime(q.at("if_modified_since_ms").get<int64_t>()));
            if(q.contains("if_unmodified_since_ms")) request.SetIfUnmodifiedSince(Aws::Utils::DateTime(q.at("if_unmodified_since_ms").get<int64_t>()));
            if (q.contains("offset")) {
                auto offset = q.at("offset").get<int64_t>();
                request.SetRange("bytes="+std::to_string(offset)+"-"+std::to_string(offset+q.at("length").get<int64_t>()-1));
            }
            if(q.contains("range_header")) request.SetRange(q.at("range_header").get<std::string>());
            auto outcome = client->GetObject(request);
            checked(outcome);
            save_response_body(outcome.GetResult().GetBody(), q.at("output_path").get<std::string>());
            const auto& object=outcome.GetResult();
            json observed={{"key",key},{"size",object.GetContentLength()},{"last_modified_ms",object.GetLastModified().Millis()},
                {"etag",std::string(object.GetETag().c_str())},{"content_type",std::string(object.GetContentType().c_str())},{"metadata",object.GetMetadata()}};
            if(op=="get_http") {
                if(!http.contains("status_code")) throw std::runtime_error("SDK HTTP observation unavailable");
                observed.update(http);
            }
            return observed;
        } else if ((op == "head" || op == "head_metadata" || op == "head_http")) {
            Aws::S3::Model::HeadObjectRequest request;
            request.SetBucket(bucket); request.SetKey(key);
            json http;HttpObservationScope observation(op=="head_http"?&http:nullptr);
            auto outcome = client->HeadObject(request); checked(outcome);
            json observed={{"key",key},{"size", outcome.GetResult().GetContentLength()},{"last_modified_ms",outcome.GetResult().GetLastModified().Millis()},{"etag",std::string(outcome.GetResult().GetETag().c_str())},{"content_type",std::string(outcome.GetResult().GetContentType().c_str())},{"metadata",outcome.GetResult().GetMetadata()}};
            if(op=="head_http") {if(!http.contains("status_code")) throw std::runtime_error("SDK HTTP observation unavailable");observed.update(http);}
            return observed;
        } else if (op == "delete") {
            Aws::S3::Model::DeleteObjectRequest request;
            request.SetBucket(bucket); request.SetKey(key); checked(client->DeleteObject(request));
        } else if (op == "list") {
            Aws::S3::Model::ListObjectsV2Request request;
            request.SetBucket(bucket); request.SetPrefix(q.at("prefix").get<std::string>());
            json keys = json::array();
            for (;;) {
                auto outcome = client->ListObjectsV2(request); checked(outcome);
                for (const auto& object : outcome.GetResult().GetContents()) keys.push_back(std::string(object.GetKey().c_str()));
                if (!outcome.GetResult().GetIsTruncated()) break;
                auto next = outcome.GetResult().GetNextContinuationToken();
                if (next.empty() || next == request.GetContinuationToken()) throw std::runtime_error("invalid pagination token");
                request.SetContinuationToken(next);
            }
            return {{"keys", keys}};
        } else if (op == "mp_create") {
            Aws::S3::Model::CreateMultipartUploadRequest request;
            request.SetBucket(bucket); request.SetKey(key);
            auto outcome = client->CreateMultipartUpload(request); checked(outcome);
            return {{"upload_id", std::string(outcome.GetResult().GetUploadId().c_str())}};
        } else if (op == "mp_part") {
            Aws::S3::Model::UploadPartRequest request;
            request.SetBucket(bucket); request.SetKey(key);
            request.SetUploadId(q.at("upload_id").get<std::string>());
            request.SetPartNumber(q.at("part_number").get<int>());
            auto stream = Aws::MakeShared<Aws::FStream>("osv", q.at("body_path").get<std::string>(), std::ios::in | std::ios::binary);
            if (!*stream) throw std::runtime_error("cannot open body_path");
            stream->seekg(0, std::ios::end); request.SetContentLength(stream->tellg()); stream->seekg(0);
            request.SetBody(stream);
            auto outcome = client->UploadPart(request); checked(outcome);
            return {{"etag", std::string(outcome.GetResult().GetETag().c_str())}};
        } else if (op == "mp_complete") {
            Aws::S3::Model::CompleteMultipartUploadRequest request;
            request.SetBucket(bucket); request.SetKey(key);
            request.SetUploadId(q.at("upload_id").get<std::string>());
            Aws::S3::Model::CompletedMultipartUpload upload;
            for (const auto& part : q.at("parts")) {
                Aws::S3::Model::CompletedPart p;
                p.SetPartNumber(part.at("part_number").get<int>()); p.SetETag(part.at("etag").get<std::string>());
                upload.AddParts(p);
            }
            request.SetMultipartUpload(upload); checked(client->CompleteMultipartUpload(request));
        } else if (op == "mp_abort") {
            Aws::S3::Model::AbortMultipartUploadRequest request;
            request.SetBucket(bucket); request.SetKey(key); request.SetUploadId(q.at("upload_id").get<std::string>());
            checked(client->AbortMultipartUpload(request));
        } else throw std::runtime_error("unsupported operation: "+op);
        return json::object();
    }
};

int main() {
    Aws::SDKOptions options;
    options.httpOptions.httpClientFactory_create_fn=[](){return Aws::MakeShared<ObservedHttpFactory>("osv");};
    Aws::InitAPI(options);
    int code = 0;
    try {
        S3Worker worker;
        for (std::string line; std::getline(std::cin, line);) {
            auto start = std::chrono::steady_clock::now();
            json response;
            try {
                auto request = json::parse(line); start=std::chrono::steady_clock::now(); response = worker.execute(request);
                if (request.at("op") == "info") {
                    response["protocol_version"] = 1;
                    response["capabilities"] = {"put", "get", "head", "list", "delete", "mp_create", "mp_part", "mp_complete", "mp_abort", "bucket_create", "list_page", "get_conditional","put_metadata","get_metadata","head_metadata","copy","copy_replace","get_http","tags_put","tags_get","tags_delete","put_http","head_http","bucket_create_raw","bucket_head","bucket_delete","bucket_location","list_v1_page","list_v2_observed"};
                    response["endpoint"] = env("OSV_ENDPOINT"); response["bucket"] = env("OSV_BUCKET");
                    response["region"] = env("OSV_REGION", "us-east-1"); response["retries"] = env("OSV_RETRIES", "0");
                    response["connections_per_worker"] = env("OSV_CONNECTIONS", "16");
                }
                response["ok"] = true;
            } catch (const SdkError& error) {
                response = {{"ok", false}, {"error", error.what()}, {"error_kind", error.status == 404 ? "not_found" : "sdk_error"}, {"status_code",error.status},{"error_code",error.error_code}};
            } catch (const std::exception& error) {
                response = {{"ok", false}, {"error", error.what()}, {"error_kind", "worker_error"}};
            }
            response["elapsed_ns"] = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count();
            std::cout << response.dump() << std::endl;
        }
    } catch (const std::exception& error) { std::cerr << error.what() << std::endl; code = 2; }
    Aws::ShutdownAPI(options);
    return code;
}
