#pragma once
#include <aws/core/http/HttpTypes.h>
#include <aws/core/utils/StringUtils.h>

/**
 * Expresses a source version without treating its query separator as part of an object key.
 * Lets the official request serialize the bucket/key path, then appends a separately
 * encoded version parameter before the official signer and HTTP client consume it.
 * The pinned SDK encodes its entire CopySource as a path and has no sourceVersionId setter.
 */
template<class Request>
class VersionedCopyRequest : public Request {
public:
    void SetSourceVersion(const Aws::String& version) { source_version=version;has_source_version=true; }
    Aws::Http::HeaderValueCollection GetRequestSpecificHeaders() const override {
        auto headers=Request::GetRequestSpecificHeaders();
        if(has_source_version)headers["x-amz-copy-source"]+="?versionId="+Aws::Utils::StringUtils::URLEncode(source_version.c_str());
        return headers;
    }
private:
    Aws::String source_version;
    bool has_source_version=false;
};
