#pragma once
#include <aws/s3/model/GetBucketVersioningRequest.h>
#include <aws/s3/model/PutBucketVersioningRequest.h>
#include <aws/s3/model/ListObjectVersionsRequest.h>

/** Delegate version-specific IO to genuine AWS SDK operations, preserving returned IDs and raw response presence. */
inline bool is_version_operation(const std::string& op) {
    return op=="versioning_get" || op=="versioning_put" || op=="versions_list" || op=="put_version" || op=="get_version" || op=="head_version" || op=="delete_version";
}

inline json execute_version_operation(Aws::S3::S3Client& client,const json& q,const std::string& bucket,const std::string& key) {
    const std::string op=q.at("op");
    json observed;HttpObservationScope observation(&observed,op=="versions_list");
    auto require_http=[&](){if(!observed.contains("status_code"))throw std::runtime_error("SDK HTTP observation unavailable");};
    if(op=="versioning_get") {
        Aws::S3::Model::GetBucketVersioningRequest request;request.SetBucket(bucket);
        auto outcome=client.GetBucketVersioning(request);checked(outcome);require_http();const auto& result=outcome.GetResult();
        if(result.GetStatus()!=Aws::S3::Model::BucketVersioningStatus::NOT_SET) observed["versioning_status"]=std::string(Aws::S3::Model::BucketVersioningStatusMapper::GetNameForBucketVersioningStatus(result.GetStatus()).c_str());
        if(result.GetMFADelete()!=Aws::S3::Model::MFADeleteStatus::NOT_SET) observed["mfa_delete"]=std::string(Aws::S3::Model::MFADeleteStatusMapper::GetNameForMFADeleteStatus(result.GetMFADelete()).c_str());
    } else if(op=="versioning_put") {
        Aws::S3::Model::VersioningConfiguration configuration;
        configuration.SetStatus(Aws::S3::Model::BucketVersioningStatusMapper::GetBucketVersioningStatusForName(q.at("status").get<std::string>()));
        if(q.contains("mfa_delete")) configuration.SetMFADelete(Aws::S3::Model::MFADeleteMapper::GetMFADeleteForName(q.at("mfa_delete").get<std::string>()));
        Aws::S3::Model::PutBucketVersioningRequest request;request.SetBucket(bucket);request.SetVersioningConfiguration(configuration);
        checked(client.PutBucketVersioning(request));require_http();
    } else if(op=="versions_list") {
        Aws::S3::Model::ListObjectVersionsRequest request;request.SetBucket(bucket);
        if(q.contains("prefix"))request.SetPrefix(q.at("prefix").get<std::string>());
        if(q.contains("delimiter"))request.SetDelimiter(q.at("delimiter").get<std::string>());
        if(q.contains("key_marker"))request.SetKeyMarker(q.at("key_marker").get<std::string>());
        if(q.contains("version_id_marker"))request.SetVersionIdMarker(q.at("version_id_marker").get<std::string>());
        if(q.contains("max_keys"))request.SetMaxKeys(q.at("max_keys").get<int>());
        auto outcome=client.ListObjectVersions(request);checked(outcome);require_http();const auto& result=outcome.GetResult();
        if(observed.value("xml_root","")!="ListVersionsResult")throw std::runtime_error("actual response is not a valid ListVersionsResult");
        auto present=[&](const std::string& name){return std::find(observed["fields"].begin(),observed["fields"].end(),json(name))!=observed["fields"].end();};
        const bool versions_present=present("Version"),markers_present=present("DeleteMarker"),name_present=present("Name");
        if(present("Name"))observed["bucket_name"]=std::string(result.GetName().c_str());
        json versions=json::array(),markers=json::array();
        for(const auto& item:result.GetVersions()) {
            json row={{"key",std::string(item.GetKey().c_str())},{"version_id",std::string(item.GetVersionId().c_str())},{"is_latest",item.GetIsLatest()},{"size",item.GetSize()},{"etag",std::string(item.GetETag().c_str())},{"last_modified_ms",item.GetLastModified().Millis()}};
            if(item.OwnerHasBeenSet())row["owner"]={{"id",std::string(item.GetOwner().GetID().c_str())},{"display_name",std::string(item.GetOwner().GetDisplayName().c_str())}};
            if(item.StorageClassHasBeenSet())row["storage_class"]=std::string(Aws::S3::Model::ObjectVersionStorageClassMapper::GetNameForObjectVersionStorageClass(item.GetStorageClass()).c_str());
            versions.push_back(row);
        }
        for(const auto& item:result.GetDeleteMarkers()) {
            json row={{"key",std::string(item.GetKey().c_str())},{"version_id",std::string(item.GetVersionId().c_str())},{"is_latest",item.GetIsLatest()},{"last_modified_ms",item.GetLastModified().Millis()}};
            if(item.OwnerHasBeenSet())row["owner"]={{"id",std::string(item.GetOwner().GetID().c_str())},{"display_name",std::string(item.GetOwner().GetDisplayName().c_str())}};
            markers.push_back(row);
        }
        observed["truncated"]=present("IsTruncated")?json(result.GetIsTruncated()):json(nullptr);
        observed["key_marker"]=present("KeyMarker")?json(std::string(result.GetKeyMarker().c_str())):json(nullptr);
        observed["version_id_marker"]=present("VersionIdMarker")?json(std::string(result.GetVersionIdMarker().c_str())):json(nullptr);
        observed["next_key_marker"]=present("NextKeyMarker")?json(std::string(result.GetNextKeyMarker().c_str())):json(nullptr);
        observed["next_version_id_marker"]=present("NextVersionIdMarker")?json(std::string(result.GetNextVersionIdMarker().c_str())):json(nullptr);
        observed["fields"]=json::array();if(name_present)observed["fields"].push_back("Name");if(versions_present)observed["fields"].push_back("Versions");if(markers_present)observed["fields"].push_back("DeleteMarkers");
        observed["versions"]=versions;observed["delete_markers"]=markers;
    } else if(op=="put_version") {
        Aws::S3::Model::PutObjectRequest request;request.SetBucket(bucket);request.SetKey(key);
        auto stream=Aws::MakeShared<Aws::FStream>("osv-version",q.at("body_path").get<std::string>(),std::ios::in|std::ios::binary);
        if(!*stream)throw std::runtime_error("cannot open body_path");
        stream->seekg(0,std::ios::end);request.SetContentLength(stream->tellg());stream->seekg(0);request.SetBody(stream);
        auto outcome=client.PutObject(request);checked(outcome);require_http();observed["etag"]=std::string(outcome.GetResult().GetETag().c_str());
        if(!outcome.GetResult().GetVersionId().empty())observed["version_id"]=std::string(outcome.GetResult().GetVersionId().c_str());
    } else if(op=="delete_version") {
        Aws::S3::Model::DeleteObjectRequest request;request.SetBucket(bucket);request.SetKey(key);
        if(q.contains("version_id"))request.SetVersionId(q.at("version_id").get<std::string>());
        auto outcome=client.DeleteObject(request);checked(outcome);require_http();
        if(!outcome.GetResult().GetVersionId().empty())observed["version_id"]=std::string(outcome.GetResult().GetVersionId().c_str());
        if(observed["http_headers"].contains("x-amz-delete-marker"))observed["delete_marker"]=outcome.GetResult().GetDeleteMarker();
    } else if(op=="get_version") {
        Aws::S3::Model::GetObjectRequest request;request.SetBucket(bucket);request.SetKey(key);
        if(q.contains("version_id"))request.SetVersionId(q.at("version_id").get<std::string>());
        if(q.contains("offset"))request.SetRange("bytes="+std::to_string(q.at("offset").get<int64_t>())+"-"+std::to_string(q.at("offset").get<int64_t>()+q.at("length").get<int64_t>()-1));
        auto outcome=client.GetObject(request);checked(outcome);require_http();const auto& result=outcome.GetResult();
        save_response_body(outcome.GetResult().GetBody(),q.at("output_path").get<std::string>());
        observed.update({{"key",key},{"size",result.GetContentLength()},{"etag",std::string(result.GetETag().c_str())},{"metadata",result.GetMetadata()},{"content_type",std::string(result.GetContentType().c_str())}});
        if(!result.GetVersionId().empty())observed["version_id"]=std::string(result.GetVersionId().c_str());
        if(observed["http_headers"].contains("x-amz-delete-marker"))observed["delete_marker"]=result.GetDeleteMarker();
    } else if(op=="head_version") {
        Aws::S3::Model::HeadObjectRequest request;request.SetBucket(bucket);request.SetKey(key);
        if(q.contains("version_id"))request.SetVersionId(q.at("version_id").get<std::string>());
        auto outcome=client.HeadObject(request);checked(outcome);require_http();const auto& result=outcome.GetResult();
        observed.update({{"key",key},{"size",result.GetContentLength()},{"etag",std::string(result.GetETag().c_str())}});
        if(observed["http_headers"].contains("last-modified"))observed["last_modified_ms"]=result.GetLastModified().Millis();
        if(!result.GetVersionId().empty())observed["version_id"]=std::string(result.GetVersionId().c_str());
        if(observed["http_headers"].contains("x-amz-delete-marker"))observed["delete_marker"]=result.GetDeleteMarker();
    }
    return observed;
}
