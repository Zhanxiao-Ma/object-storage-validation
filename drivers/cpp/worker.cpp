#include "stream_io.h"
#include "protocol_input.h"
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
#include <aws/s3/model/DeleteObjectsRequest.h>
#include <aws/s3/model/GetObjectRequest.h>
#include <aws/s3/model/HeadObjectRequest.h>
#include <aws/s3/model/ListObjectsV2Request.h>
#include <aws/s3/model/ListObjectsRequest.h>
#include <aws/s3/model/PutObjectRequest.h>
#include <aws/s3/model/UploadPartRequest.h>
#include <aws/s3/model/UploadPartCopyRequest.h>
#include <aws/s3/model/ListMultipartUploadsRequest.h>
#include <aws/core/utils/StringUtils.h>
#include <nlohmann/json.hpp>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>

using json = nlohmann::json;

/** Supply literal per-request encryption headers through the public SDK API, avoiding model MD5 autofill. */
template<class Request> static void encryption_headers(Request& request,const json& q) {
    if(q.contains("sse_headers") && q.contains("encryption_headers")) throw std::runtime_error("ambiguous encryption headers");
    const auto& headers=q.at(q.contains("encryption_headers")?"encryption_headers":"sse_headers");
    if(!headers.is_object()) throw std::runtime_error("sse_headers must be an object");
    for(auto it=headers.begin();it!=headers.end();++it) {
        if((it.key()!="x-amz-server-side-encryption-customer-algorithm" &&
            it.key()!="x-amz-server-side-encryption-customer-key" &&
            it.key()!="x-amz-server-side-encryption-customer-key-md5" && it.key()!="content-type" && it.key()!="x-amz-server-side-encryption" &&
            it.key()!="x-amz-server-side-encryption-aws-kms-key-id") || !it.value().is_string())
            throw std::runtime_error("invalid encryption header: "+it.key());
        request.SetAdditionalCustomHeaderValue(it.key().c_str(),it.value().get<std::string>().c_str());
    }
}

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
    json headers;
    SdkError(int code, const std::string& message, const std::string& name,json observed_headers=json::object()) : std::runtime_error(message), status(code), error_code(name),headers(std::move(observed_headers)) {}
};

template<class Outcome> static void checked(const Outcome& outcome) {
    if (!outcome.IsSuccess()) {
        auto& error = outcome.GetError();
        json headers=json::object();for(const auto& entry:error.GetResponseHeaders()) headers[std::string(entry.first.c_str())]=std::string(entry.second.c_str());
        throw SdkError(static_cast<int>(error.GetResponseCode()), error.GetMessage().c_str(), error.GetExceptionName().c_str(),headers);
    }
}

#include "versioning_ops.h"
#include "bucket_api_generated.h"

#include "wire_headers.h"
#include "s3_identities.h"

/**
 * Runs AWS C++ SDK S3 operations for direct comparison with other C++ adapters.
 * A persistent client excludes process startup; each timed operation includes payload I/O.
 */
class S3Worker {
    std::string bucket = required("OSV_BUCKET");
    S3Identities identities;
    std::shared_ptr<Aws::S3::S3Client> client;
public:
    S3Worker():client(identities.get("main")) {}
    json execute(const json& q) {
        std::unique_ptr<WireHeaderScope> header_scope;
        if(q.contains("wire_headers"))header_scope=std::make_unique<WireHeaderScope>(q.at("wire_headers"));
        auto client=(q.contains("identity")||q.contains("wire_headers"))?identities.select(q):this->client;
        std::string op = q.at("op"), key = q.value("key", "");
        if(op=="put_encryption")op="put_sse_c";
        else if(op=="get_encryption")op="get_sse_c";
        else if(op=="head_encryption")op="head_sse_c";
        const std::string& bucket=q.contains("bucket")?q.at("bucket").get_ref<const std::string&>():this->bucket;
        if (op == "info") return {{"backend", "s3"}, {"api", "s3"}, {"language", "cpp"}, {"sdk", "aws-sdk-cpp"},
            {"sdk_version", AWS_SDK_VERSION_STRING}, {"measurement", "operation including local payload IO; excludes JSONL serialization"}};
        if(is_version_operation(op))return execute_version_operation(*client,q,bucket,key);
        if(osv_bucket_api::handles(op))return osv_bucket_api::execute(*client,q);
        if(op=="delete_many" || op=="delete_many_versions") {
            Aws::S3::Model::DeleteObjectsRequest request;request.SetBucket(bucket);Aws::S3::Model::Delete deletion;
            for(const auto& item:q.at("objects")) {
                Aws::S3::Model::ObjectIdentifier id;id.SetKey(item.at("key").get<std::string>());
                if(item.contains("version_id"))id.SetVersionId(item.at("version_id").get<std::string>());deletion.AddObjects(id);
            }
            if(q.contains("quiet"))deletion.SetQuiet(q.at("quiet").get<bool>());request.SetDelete(deletion);
            json observed;HttpObservationScope observation(&observed,true);
            auto outcome=client->DeleteObjects(request);checked(outcome);
            if(observed.value("xml_root",std::string())!="DeleteResult") throw std::runtime_error("actual response is not a valid DeleteResult");
            json deleted=json::array(),errors=json::array();
            for(const auto& item:outcome.GetResult().GetDeleted()) {
                json row=json::object();
                if(item.KeyHasBeenSet())row["key"]=std::string(item.GetKey().c_str());if(item.VersionIdHasBeenSet())row["version_id"]=std::string(item.GetVersionId().c_str());
                if(item.DeleteMarkerHasBeenSet())row["delete_marker"]=item.GetDeleteMarker();if(item.DeleteMarkerVersionIdHasBeenSet())row["delete_marker_version_id"]=std::string(item.GetDeleteMarkerVersionId().c_str());deleted.push_back(row);
            }
            for(const auto& item:outcome.GetResult().GetErrors()) {
                json row=json::object();
                if(item.KeyHasBeenSet())row["key"]=std::string(item.GetKey().c_str());if(item.VersionIdHasBeenSet())row["version_id"]=std::string(item.GetVersionId().c_str());
                if(item.CodeHasBeenSet())row["code"]=std::string(item.GetCode().c_str());if(item.MessageHasBeenSet())row["message"]=std::string(item.GetMessage().c_str());errors.push_back(row);
            }
            observed.update({{"deleted",deleted},{"errors",errors},{"errors_present",std::find(observed["fields"].begin(),observed["fields"].end(),json("Error"))!=observed["fields"].end()}});
            return observed;
        }
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
        if(op=="bucket_create_raw" || op=="bucket_create_locked" || op=="bucket_create_acl" || op=="bucket_head" || op=="bucket_delete" || op=="bucket_location") {
            json http;HttpObservationScope observation(&http);
            if(op=="bucket_create_raw" || op=="bucket_create_locked" || op=="bucket_create_acl") {
                Aws::S3::Model::CreateBucketRequest request;request.SetBucket(bucket);
                if(op=="bucket_create_locked") request.SetObjectLockEnabledForBucket(true);
                if(op=="bucket_create_acl") request.SetACL(Aws::S3::Model::BucketCannedACLMapper::GetBucketCannedACLForName(q.at("acl").get<std::string>()));
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
        } else if ((op == "put" || op == "put_metadata" || op == "put_http" || op == "put_sse_c" || op == "put_acl")) {
            Aws::S3::Model::PutObjectRequest request;
            request.SetBucket(bucket); request.SetKey(key);
            auto stream = Aws::MakeShared<Aws::FStream>("osv", q.at("body_path").get<std::string>(), std::ios::in | std::ios::binary);
            if (!*stream) throw std::runtime_error("cannot open body_path");
            request.SetBody(stream);
            if(op=="put_sse_c") encryption_headers(request,q);
            if(op=="put_acl") request.SetACL(Aws::S3::Model::ObjectCannedACLMapper::GetObjectCannedACLForName(q.at("acl").get<std::string>()));
            if(op=="put_metadata") {
                request.SetContentType(q.at("content_type").get<std::string>());
                for(auto it=q.at("metadata").begin();it!=q.at("metadata").end();++it) request.AddMetadata(it.key(),it.value().get<std::string>());
            }
            if(q.contains("content_length")) request.SetContentLength(q.at("content_length").get<int64_t>());
            if(q.contains("cache_control")) request.SetCacheControl(q.at("cache_control").get<std::string>());
            if(q.contains("expires_ms")) request.SetExpires(Aws::Utils::DateTime(q.at("expires_ms").get<int64_t>()));
            json http;HttpObservationScope observation((op=="put_http"||op=="put_sse_c"||op=="put_acl")?&http:nullptr);
            auto outcome=client->PutObject(request);checked(outcome);
            if(op=="put_http"||op=="put_sse_c"||op=="put_acl") {if(!http.contains("status_code")) throw std::runtime_error("SDK HTTP observation unavailable");http["etag"]=std::string(outcome.GetResult().GetETag().c_str());return http;}

        } else if ((op == "get" || op == "get_conditional" || op == "get_metadata" || op == "get_http" || op == "get_part" || op == "get_sse_c")) {
            Aws::S3::Model::GetObjectRequest request;
            request.SetBucket(bucket); request.SetKey(key);
            json http=json::object();
            HttpObservationScope observation((op=="get_http"||op=="get_sse_c")?&http:nullptr);
            if(op=="get_sse_c") encryption_headers(request,q);
            if(q.contains("if_match")) request.SetIfMatch(q.at("if_match").get<std::string>());
            if(q.contains("if_none_match")) request.SetIfNoneMatch(q.at("if_none_match").get<std::string>());
            if(q.contains("if_modified_since_ms")) request.SetIfModifiedSince(Aws::Utils::DateTime(q.at("if_modified_since_ms").get<int64_t>()));
            if(q.contains("if_unmodified_since_ms")) request.SetIfUnmodifiedSince(Aws::Utils::DateTime(q.at("if_unmodified_since_ms").get<int64_t>()));
            if (q.contains("offset")) {
                auto offset = q.at("offset").get<int64_t>();
                request.SetRange("bytes="+std::to_string(offset)+"-"+std::to_string(offset+q.at("length").get<int64_t>()-1));
            }
            if(q.contains("range_header")) request.SetRange(q.at("range_header").get<std::string>());
            if(q.contains("part_number")) request.SetPartNumber(q.at("part_number").get<int>());
            auto outcome = client->GetObject(request);
            checked(outcome);
            save_response_body(outcome.GetResult().GetBody(), q.at("output_path").get<std::string>());
            const auto& object=outcome.GetResult();
            json observed={{"key",key},{"size",object.GetContentLength()},{"last_modified_ms",object.GetLastModified().Millis()},
                {"etag",std::string(object.GetETag().c_str())},{"content_type",std::string(object.GetContentType().c_str())},{"metadata",object.GetMetadata()}};
            if(op=="get_part" || (op=="get_sse_c" && q.contains("part_number"))) observed["parts_count"]=object.GetPartsCount();
            if(op=="get_http"||op=="get_sse_c") {
                if(!http.contains("status_code")) throw std::runtime_error("SDK HTTP observation unavailable");
                observed.update(http);
            }
            return observed;
        } else if ((op == "head" || op == "head_metadata" || op == "head_http" || op == "head_part" || op == "head_sse_c")) {
            Aws::S3::Model::HeadObjectRequest request;
            request.SetBucket(bucket); request.SetKey(key);
            json http;HttpObservationScope observation((op=="head_http"||op=="head_sse_c")?&http:nullptr);
            if(op=="head_sse_c") encryption_headers(request,q);
            if(q.contains("part_number")) request.SetPartNumber(q.at("part_number").get<int>());
            auto outcome = client->HeadObject(request); checked(outcome);
            json observed={{"key",key},{"size", outcome.GetResult().GetContentLength()},{"last_modified_ms",outcome.GetResult().GetLastModified().Millis()},{"etag",std::string(outcome.GetResult().GetETag().c_str())},{"content_type",std::string(outcome.GetResult().GetContentType().c_str())},{"metadata",outcome.GetResult().GetMetadata()}};
            if(op=="head_part" || (op=="head_sse_c" && q.contains("part_number"))) observed["parts_count"]=outcome.GetResult().GetPartsCount();
            if(op=="head_http"||op=="head_sse_c") {if(!http.contains("status_code")) throw std::runtime_error("SDK HTTP observation unavailable");observed.update(http);}
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
        } else if (op == "mp_copy") {
            Aws::S3::Model::UploadPartCopyRequest request;
            request.SetBucket(bucket);request.SetKey(key);request.SetUploadId(q.at("upload_id").get<std::string>());request.SetPartNumber(q.at("part_number").get<int>());
            // This SDK's request serializer URL-encodes CopySource itself.
            std::string source=q.value("source_bucket",bucket)+"/"+q.at("source_key").get<std::string>();
            if(q.contains("version_id"))throw std::runtime_error("versioned multipart copy is not implemented by this worker");
            request.SetCopySource(source);
            if(q.contains("range_header")) request.SetCopySourceRange(q.at("range_header").get<std::string>());
            json http;HttpObservationScope observation(&http);
            auto outcome=client->UploadPartCopy(request);checked(outcome);
            if(!http.contains("status_code")) throw std::runtime_error("SDK HTTP observation unavailable");
            http["etag"]=std::string(outcome.GetResult().GetCopyPartResult().GetETag().c_str());return http;
        } else if (op == "mp_list") {
            Aws::S3::Model::ListMultipartUploadsRequest request;request.SetBucket(bucket);
            if(q.contains("prefix")) request.SetPrefix(q.at("prefix").get<std::string>());
            if(q.contains("delimiter")) request.SetDelimiter(q.at("delimiter").get<std::string>());
            if(q.contains("key_marker")) request.SetKeyMarker(q.at("key_marker").get<std::string>());
            if(q.contains("upload_id_marker")) request.SetUploadIdMarker(q.at("upload_id_marker").get<std::string>());
            if(q.contains("max_uploads")) request.SetMaxUploads(q.at("max_uploads").get<int>());
            json http;HttpObservationScope observation(&http);
            auto outcome=client->ListMultipartUploads(request);checked(outcome);const auto& result=outcome.GetResult();
            if(!http.contains("status_code")) throw std::runtime_error("SDK HTTP observation unavailable");
            json uploads=json::array(),prefixes=json::array();
            for(const auto& item:result.GetUploads()) {
                json row={{"key",std::string(item.GetKey().c_str())},{"upload_id",std::string(item.GetUploadId().c_str())},{"initiated_ms",item.GetInitiated().Millis()}};
                if(item.OwnerHasBeenSet())row["owner"]={{"id",std::string(item.GetOwner().GetID().c_str())},{"display_name",std::string(item.GetOwner().GetDisplayName().c_str())}};
                if(item.InitiatorHasBeenSet())row["initiator"]={{"id",std::string(item.GetInitiator().GetID().c_str())},{"display_name",std::string(item.GetInitiator().GetDisplayName().c_str())}};
                uploads.push_back(row);
            }
            for(const auto& item:result.GetCommonPrefixes())prefixes.push_back(std::string(item.GetPrefix().c_str()));
            http.update({{"uploads",uploads},{"common_prefixes",prefixes},{"truncated",result.GetIsTruncated()},{"next_key_marker",std::string(result.GetNextKeyMarker().c_str())},{"next_upload_id_marker",std::string(result.GetNextUploadIdMarker().c_str())},
                {"key_marker",std::string(result.GetKeyMarker().c_str())},{"upload_id_marker",std::string(result.GetUploadIdMarker().c_str())},{"max_uploads",result.GetMaxUploads()},{"prefix",std::string(result.GetPrefix().c_str())},{"delimiter",std::string(result.GetDelimiter().c_str())}});
            return http;
        } else if (op == "mp_create" || op == "mp_create_metadata" || op == "mp_create_sse_c") {
            Aws::S3::Model::CreateMultipartUploadRequest request;
            request.SetBucket(bucket); request.SetKey(key);
            if(op=="mp_create_sse_c") encryption_headers(request,q);
            if(q.contains("content_type")) request.SetContentType(q.at("content_type").get<std::string>());
            if(q.contains("metadata")) for(auto it=q.at("metadata").begin();it!=q.at("metadata").end();++it) request.AddMetadata(it.key(),it.value().get<std::string>());
            auto outcome = client->CreateMultipartUpload(request); checked(outcome);
            return {{"upload_id", std::string(outcome.GetResult().GetUploadId().c_str())}};
        } else if (op == "mp_part" || op == "mp_part_sse_c") {
            Aws::S3::Model::UploadPartRequest request;
            request.SetBucket(bucket); request.SetKey(key);
            if(op=="mp_part_sse_c") encryption_headers(request,q);
            request.SetUploadId(q.at("upload_id").get<std::string>());
            request.SetPartNumber(q.at("part_number").get<int>());
            auto stream = Aws::MakeShared<Aws::FStream>("osv", q.at("body_path").get<std::string>(), std::ios::in | std::ios::binary);
            if (!*stream) throw std::runtime_error("cannot open body_path");
            stream->seekg(0, std::ios::end); request.SetContentLength(stream->tellg()); stream->seekg(0);
            request.SetBody(stream);
            auto outcome = client->UploadPart(request); checked(outcome);
            return {{"etag", std::string(outcome.GetResult().GetETag().c_str())}};
        } else if (op == "mp_complete" || op == "mp_complete_observed" || op == "mp_complete_empty" || op == "mp_complete_sse_c") {
            Aws::S3::Model::CompleteMultipartUploadRequest request;
            request.SetBucket(bucket); request.SetKey(key);
            if(op=="mp_complete_sse_c") encryption_headers(request,q);
            request.SetUploadId(q.at("upload_id").get<std::string>());
            Aws::S3::Model::CompletedMultipartUpload upload;
            for (const auto& part : q.value("parts", json::array())) {
                Aws::S3::Model::CompletedPart p;
                p.SetPartNumber(part.at("part_number").get<int>()); p.SetETag(part.at("etag").get<std::string>());
                upload.AddParts(p);
            }
            if(op!="mp_complete_empty") request.SetMultipartUpload(upload);
            json http;HttpObservationScope observation(&http);
            auto outcome=client->CompleteMultipartUpload(request); checked(outcome);
            if(!http.contains("status_code")) throw std::runtime_error("SDK HTTP observation unavailable");
            http["etag"]=std::string(outcome.GetResult().GetETag().c_str());
            if(!outcome.GetResult().GetVersionId().empty())http["version_id"]=std::string(outcome.GetResult().GetVersionId().c_str());return http;
        } else if (op == "mp_abort" || op == "mp_abort_raw") {
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
        ProtocolInput input;
        S3Worker worker;
        for (std::string line; input.read(line);) {
            auto start = std::chrono::steady_clock::now();
            json response;
            try {
                auto request = json::parse(line); start=std::chrono::steady_clock::now(); response = worker.execute(request);
                if (request.at("op") == "info") {
                    response["protocol_version"] = 1;
                    response["capabilities"] = {"put", "get", "head", "list", "delete", "mp_create", "mp_part", "mp_complete", "mp_abort", "bucket_create", "list_page", "get_conditional","put_metadata","get_metadata","head_metadata","copy","copy_replace","get_http","tags_put","tags_get","tags_delete","put_http","head_http","put_sse_c","get_sse_c","head_sse_c","put_encryption","get_encryption","head_encryption","mp_create_sse_c","mp_part_sse_c","mp_complete_sse_c","bucket_create_raw","bucket_create_locked","bucket_create_acl","put_acl","bucket_head","bucket_delete","bucket_location","list_v1_page","list_v2_observed","delete_many","delete_many_versions","mp_create_metadata","mp_complete_observed","mp_complete_empty","mp_abort_raw","mp_copy","mp_list","get_part","head_part","versioning_get","versioning_put","versions_list","put_version","get_version","head_version","delete_version"};
                    for(const auto& identity:S3Identities::capabilities())response["capabilities"].push_back(identity);
                    for(const auto& api:osv_bucket_api::capabilities())response["capabilities"].push_back(api);
                    response["header_request_timeout_ms"]=env("OSV_HEADER_REQUEST_TIMEOUT_MS","30000");
                    response["endpoint"] = env("OSV_ENDPOINT"); response["bucket"] = env("OSV_BUCKET");
                    response["region"] = env("OSV_REGION", "us-east-1"); response["retries"] = env("OSV_RETRIES", "0");
                    response["connections_per_worker"] = env("OSV_CONNECTIONS", "16");
                }
                response["ok"] = true;
            } catch (const SdkError& error) {
                response = {{"ok", false}, {"error", error.what()}, {"error_kind", error.status == 404 ? "not_found" : "sdk_error"}, {"status_code",error.status},{"error_code",error.error_code},{"http_headers",error.headers}};
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
