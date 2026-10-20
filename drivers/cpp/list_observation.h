#pragma once
#include "http_observer.h"
#include <aws/s3/model/ListObjectsResult.h>
#include <aws/s3/model/ListObjectsV2Result.h>
#include <algorithm>
#include <type_traits>

/** Preserve SDK-decoded values while retaining actual XML field presence that generated result getters discard. */
template<class Result> nlohmann::json observed_list_page(const Result& result,nlohmann::json observed) {
    using J=nlohmann::json;
    if(!observed.contains("xml_root") || observed["xml_root"]!="ListBucketResult") throw std::runtime_error("actual response is not a valid ListBucketResult");
    auto present=[&](const char* name){return std::find(observed["fields"].begin(),observed["fields"].end(),J(name))!=observed["fields"].end();};
    J objects=J::array(),keys=J::array(),prefixes=J::array();
    for(const auto& item:result.GetContents()) {
        if(!item.KeyHasBeenSet()) throw std::runtime_error("SDK listing object has no Key");
        J row={{"key",std::string(item.GetKey().c_str())}};keys.push_back(row["key"]);
        if(item.SizeHasBeenSet()) row["size"]=item.GetSize();
        if(item.LastModifiedHasBeenSet()) row["last_modified_ms"]=item.GetLastModified().Millis();
        if(item.ETagHasBeenSet()) row["etag"]=std::string(item.GetETag().c_str());
        if(item.StorageClassHasBeenSet()) row["storage_class"]=std::string(Aws::S3::Model::ObjectStorageClassMapper::GetNameForObjectStorageClass(item.GetStorageClass()).c_str());
        if(item.OwnerHasBeenSet()) {
            J owner=J::object();
            if(item.GetOwner().IDHasBeenSet()) owner["id"]=std::string(item.GetOwner().GetID().c_str());
            if(item.GetOwner().DisplayNameHasBeenSet()) owner["display_name"]=std::string(item.GetOwner().GetDisplayName().c_str());
            row["owner"]=owner;
        }
        objects.push_back(row);
    }
    for(const auto& item:result.GetCommonPrefixes()) prefixes.push_back(std::string(item.GetPrefix().c_str()));
    observed.update({{"keys",keys},{"objects",objects},{"common_prefixes",prefixes}});
    observed["prefix"]=present("Prefix")?J(std::string(result.GetPrefix().c_str())):J(nullptr);
    observed["delimiter"]=present("Delimiter")?J(std::string(result.GetDelimiter().c_str())):J(nullptr);
    observed["max_keys"]=present("MaxKeys")?J(result.GetMaxKeys()):J(nullptr);
    observed["truncated"]=present("IsTruncated")?J(result.GetIsTruncated()):J(nullptr);
    if constexpr(std::is_same_v<Result,Aws::S3::Model::ListObjectsV2Result>) {
        observed["key_count"]=present("KeyCount")?J(result.GetKeyCount()):J(nullptr);
        observed["token"]=present("ContinuationToken")?J(std::string(result.GetContinuationToken().c_str())):J(nullptr);
        observed["next_token"]=present("NextContinuationToken")?J(std::string(result.GetNextContinuationToken().c_str())):J(nullptr);
        observed["start_after"]=present("StartAfter")?J(std::string(result.GetStartAfter().c_str())):J(nullptr);
    } else {
        observed["marker"]=present("Marker")?J(std::string(result.GetMarker().c_str())):J(nullptr);
        observed["next_marker"]=present("NextMarker")?J(std::string(result.GetNextMarker().c_str())):J(nullptr);
    }
    return observed;
}
