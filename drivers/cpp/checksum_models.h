#pragma once
#include <nlohmann/json.hpp>
#include <type_traits>
#include <utility>
#include <stdexcept>
#include <aws/s3/model/PutObjectRequest.h>
#include <aws/s3/model/CreateMultipartUploadRequest.h>
#include <aws/s3/model/ChecksumAlgorithm.h>
#include <aws/s3/model/ChecksumType.h>
#include <aws/s3/model/ChecksumMode.h>

/** Preserve actual typed checksum support and optional fields across request/result models.
 * Detection traits call only setters/getters supplied by the compiled SDK; unsupported
 * inputs fail before transport and response absence remains absence in the report.
 */
namespace osv_checksums {
using json=nlohmann::json;
/** Detect whether the real request model can set ChecksumCRC32, avoiding synthetic support. */
template<class T,class=void> struct can_set_ChecksumCRC32:std::false_type {};
/** Bind the actual public ChecksumCRC32 setter only when it exists in this SDK model. */
template<class T> struct can_set_ChecksumCRC32<T,std::void_t<decltype(std::declval<T&>().SetChecksumCRC32(std::declval<const Aws::String&>()))>>:std::true_type {};
/** Detect whether the real result exposes ChecksumCRC32, retaining model-specific capabilities. */
template<class T,class=void> struct can_get_ChecksumCRC32:std::false_type {};
/** Bind only an actual public SDK getter for ChecksumCRC32. */
template<class T> struct can_get_ChecksumCRC32<T,std::void_t<decltype(std::declval<const T&>().GetChecksumCRC32())>>:std::true_type {};
/** Detect whether the real request model can set ChecksumCRC32C, avoiding synthetic support. */
template<class T,class=void> struct can_set_ChecksumCRC32C:std::false_type {};
/** Bind the actual public ChecksumCRC32C setter only when it exists in this SDK model. */
template<class T> struct can_set_ChecksumCRC32C<T,std::void_t<decltype(std::declval<T&>().SetChecksumCRC32C(std::declval<const Aws::String&>()))>>:std::true_type {};
/** Detect whether the real result exposes ChecksumCRC32C, retaining model-specific capabilities. */
template<class T,class=void> struct can_get_ChecksumCRC32C:std::false_type {};
/** Bind only an actual public SDK getter for ChecksumCRC32C. */
template<class T> struct can_get_ChecksumCRC32C<T,std::void_t<decltype(std::declval<const T&>().GetChecksumCRC32C())>>:std::true_type {};
/** Detect whether the real request model can set ChecksumCRC64NVME, avoiding synthetic support. */
template<class T,class=void> struct can_set_ChecksumCRC64NVME:std::false_type {};
/** Bind the actual public ChecksumCRC64NVME setter only when it exists in this SDK model. */
template<class T> struct can_set_ChecksumCRC64NVME<T,std::void_t<decltype(std::declval<T&>().SetChecksumCRC64NVME(std::declval<const Aws::String&>()))>>:std::true_type {};
/** Detect whether the real result exposes ChecksumCRC64NVME, retaining model-specific capabilities. */
template<class T,class=void> struct can_get_ChecksumCRC64NVME:std::false_type {};
/** Bind only an actual public SDK getter for ChecksumCRC64NVME. */
template<class T> struct can_get_ChecksumCRC64NVME<T,std::void_t<decltype(std::declval<const T&>().GetChecksumCRC64NVME())>>:std::true_type {};
/** Detect whether the real request model can set ChecksumSHA1, avoiding synthetic support. */
template<class T,class=void> struct can_set_ChecksumSHA1:std::false_type {};
/** Bind the actual public ChecksumSHA1 setter only when it exists in this SDK model. */
template<class T> struct can_set_ChecksumSHA1<T,std::void_t<decltype(std::declval<T&>().SetChecksumSHA1(std::declval<const Aws::String&>()))>>:std::true_type {};
/** Detect whether the real result exposes ChecksumSHA1, retaining model-specific capabilities. */
template<class T,class=void> struct can_get_ChecksumSHA1:std::false_type {};
/** Bind only an actual public SDK getter for ChecksumSHA1. */
template<class T> struct can_get_ChecksumSHA1<T,std::void_t<decltype(std::declval<const T&>().GetChecksumSHA1())>>:std::true_type {};
/** Detect whether the real request model can set ChecksumSHA256, avoiding synthetic support. */
template<class T,class=void> struct can_set_ChecksumSHA256:std::false_type {};
/** Bind the actual public ChecksumSHA256 setter only when it exists in this SDK model. */
template<class T> struct can_set_ChecksumSHA256<T,std::void_t<decltype(std::declval<T&>().SetChecksumSHA256(std::declval<const Aws::String&>()))>>:std::true_type {};
/** Detect whether the real result exposes ChecksumSHA256, retaining model-specific capabilities. */
template<class T,class=void> struct can_get_ChecksumSHA256:std::false_type {};
/** Bind only an actual public SDK getter for ChecksumSHA256. */
template<class T> struct can_get_ChecksumSHA256<T,std::void_t<decltype(std::declval<const T&>().GetChecksumSHA256())>>:std::true_type {};
/** Detect whether the real request model can set ChecksumAlgorithm, avoiding synthetic support. */
template<class T,class=void> struct can_set_ChecksumAlgorithm:std::false_type {};
/** Bind the actual public ChecksumAlgorithm setter only when it exists in this SDK model. */
template<class T> struct can_set_ChecksumAlgorithm<T,std::void_t<decltype(std::declval<T&>().SetChecksumAlgorithm(std::declval<Aws::S3::Model::ChecksumAlgorithm>()))>>:std::true_type {};
/** Detect whether the real result exposes ChecksumAlgorithm, retaining model-specific capabilities. */
template<class T,class=void> struct can_get_ChecksumAlgorithm:std::false_type {};
/** Bind only an actual public SDK getter for ChecksumAlgorithm. */
template<class T> struct can_get_ChecksumAlgorithm<T,std::void_t<decltype(std::declval<const T&>().GetChecksumAlgorithm())>>:std::true_type {};
/** Detect whether the real request model can set ChecksumType, avoiding synthetic support. */
template<class T,class=void> struct can_set_ChecksumType:std::false_type {};
/** Bind the actual public ChecksumType setter only when it exists in this SDK model. */
template<class T> struct can_set_ChecksumType<T,std::void_t<decltype(std::declval<T&>().SetChecksumType(std::declval<Aws::S3::Model::ChecksumType>()))>>:std::true_type {};
/** Detect whether the real result exposes ChecksumType, retaining model-specific capabilities. */
template<class T,class=void> struct can_get_ChecksumType:std::false_type {};
/** Bind only an actual public SDK getter for ChecksumType. */
template<class T> struct can_get_ChecksumType<T,std::void_t<decltype(std::declval<const T&>().GetChecksumType())>>:std::true_type {};
/** Detect whether the real request model can set ChecksumMode, avoiding synthetic support. */
template<class T,class=void> struct can_set_ChecksumMode:std::false_type {};
/** Bind the actual public ChecksumMode setter only when it exists in this SDK model. */
template<class T> struct can_set_ChecksumMode<T,std::void_t<decltype(std::declval<T&>().SetChecksumMode(std::declval<Aws::S3::Model::ChecksumMode>()))>>:std::true_type {};
/** Detect whether the real result exposes ChecksumMode, retaining model-specific capabilities. */
template<class T,class=void> struct can_get_ChecksumMode:std::false_type {};
/** Bind only an actual public SDK getter for ChecksumMode. */
template<class T> struct can_get_ChecksumMode<T,std::void_t<decltype(std::declval<const T&>().GetChecksumMode())>>:std::true_type {};

template<class Request> void set(Request& request,const std::string& name,const json& value) {
    if(!value.is_string())throw std::invalid_argument("checksum member must be textual: "+name);
    const auto text=value.get<std::string>();
    if(name=="ChecksumCRC32") {
        if constexpr(can_set_ChecksumCRC32<Request>::value){request.SetChecksumCRC32(Aws::String(text.c_str(),text.size()));return;}
        else throw std::invalid_argument("SDK model has no member "+name);
    }
    if(name=="ChecksumCRC32C") {
        if constexpr(can_set_ChecksumCRC32C<Request>::value){request.SetChecksumCRC32C(Aws::String(text.c_str(),text.size()));return;}
        else throw std::invalid_argument("SDK model has no member "+name);
    }
    if(name=="ChecksumCRC64NVME") {
        if constexpr(can_set_ChecksumCRC64NVME<Request>::value){request.SetChecksumCRC64NVME(Aws::String(text.c_str(),text.size()));return;}
        else throw std::invalid_argument("SDK model has no member "+name);
    }
    if(name=="ChecksumSHA1") {
        if constexpr(can_set_ChecksumSHA1<Request>::value){request.SetChecksumSHA1(Aws::String(text.c_str(),text.size()));return;}
        else throw std::invalid_argument("SDK model has no member "+name);
    }
    if(name=="ChecksumSHA256") {
        if constexpr(can_set_ChecksumSHA256<Request>::value){request.SetChecksumSHA256(Aws::String(text.c_str(),text.size()));return;}
        else throw std::invalid_argument("SDK model has no member "+name);
    }
    if(name=="ChecksumAlgorithm") {
        if constexpr(can_set_ChecksumAlgorithm<Request>::value){request.SetChecksumAlgorithm(Aws::S3::Model::ChecksumAlgorithmMapper::GetChecksumAlgorithmForName(Aws::String(text.c_str(),text.size())));return;}
        else throw std::invalid_argument("SDK model has no member "+name);
    }
    if(name=="ChecksumType") {
        if constexpr(can_set_ChecksumType<Request>::value){request.SetChecksumType(Aws::S3::Model::ChecksumTypeMapper::GetChecksumTypeForName(Aws::String(text.c_str(),text.size())));return;}
        else throw std::invalid_argument("SDK model has no member "+name);
    }
    if(name=="ChecksumMode") {
        if constexpr(can_set_ChecksumMode<Request>::value){request.SetChecksumMode(Aws::S3::Model::ChecksumModeMapper::GetChecksumModeForName(Aws::String(text.c_str(),text.size())));return;}
        else throw std::invalid_argument("SDK model has no member "+name);
    }
    throw std::invalid_argument("unknown checksum member "+name);
}
template<class Request> void apply(Request& request,const json& q) {
    if(q.contains("checksum")) {
        const auto& checksum=q.at("checksum");
        if(!checksum.is_object())throw std::invalid_argument("checksum must be an object");
        for(auto item=checksum.begin();item!=checksum.end();++item) {
            if(item.key()=="ChecksumMode")throw std::invalid_argument("ChecksumMode must use checksum_mode");
            set(request,item.key(),item.value());
        }
    }
    if(q.contains("checksum_mode"))set(request,"ChecksumMode",q.at("checksum_mode"));
}
inline bool field_present(const json& existing,const std::string& name,const std::string& header) {
    if(existing.contains("http_headers")&&existing.at("http_headers").is_object()&&existing.at("http_headers").contains(header))return true;
    if(existing.contains("fields")&&existing.at("fields").is_array())
        for(const auto& field:existing.at("fields"))if(field==name)return true;
    return false;
}
template<class Response> json observe(json existing,const Response& response) {
    json checksum=json::object();
    if constexpr(can_get_ChecksumCRC32<Response>::value) {
        const auto& value=response.GetChecksumCRC32();
        if(field_present(existing,"ChecksumCRC32","x-amz-checksum-crc32"))checksum["ChecksumCRC32"]=std::string(value.c_str(),value.size());
    }
    if constexpr(can_get_ChecksumCRC32C<Response>::value) {
        const auto& value=response.GetChecksumCRC32C();
        if(field_present(existing,"ChecksumCRC32C","x-amz-checksum-crc32c"))checksum["ChecksumCRC32C"]=std::string(value.c_str(),value.size());
    }
    if constexpr(can_get_ChecksumCRC64NVME<Response>::value) {
        const auto& value=response.GetChecksumCRC64NVME();
        if(field_present(existing,"ChecksumCRC64NVME","x-amz-checksum-crc64nvme"))checksum["ChecksumCRC64NVME"]=std::string(value.c_str(),value.size());
    }
    if constexpr(can_get_ChecksumSHA1<Response>::value) {
        const auto& value=response.GetChecksumSHA1();
        if(field_present(existing,"ChecksumSHA1","x-amz-checksum-sha1"))checksum["ChecksumSHA1"]=std::string(value.c_str(),value.size());
    }
    if constexpr(can_get_ChecksumSHA256<Response>::value) {
        const auto& value=response.GetChecksumSHA256();
        if(field_present(existing,"ChecksumSHA256","x-amz-checksum-sha256"))checksum["ChecksumSHA256"]=std::string(value.c_str(),value.size());
    }
    if constexpr(can_get_ChecksumAlgorithm<Response>::value) {
        const auto value=response.GetChecksumAlgorithm();
        if(field_present(existing,"ChecksumAlgorithm","x-amz-checksum-algorithm"))checksum["ChecksumAlgorithm"]=Aws::S3::Model::ChecksumAlgorithmMapper::GetNameForChecksumAlgorithm(value).c_str();
    }
    if constexpr(can_get_ChecksumType<Response>::value) {
        const auto value=response.GetChecksumType();
        if(field_present(existing,"ChecksumType","x-amz-checksum-type"))checksum["ChecksumType"]=Aws::S3::Model::ChecksumTypeMapper::GetNameForChecksumType(value).c_str();
    }
    existing["checksum"]=std::move(checksum);return existing;
}
inline json capabilities() {
    json result=json::array();
    if constexpr(can_set_ChecksumCRC32<Aws::S3::Model::PutObjectRequest>::value)result.push_back("checksum_crc32");
    if constexpr(can_set_ChecksumCRC32C<Aws::S3::Model::PutObjectRequest>::value)result.push_back("checksum_crc32c");
    if constexpr(can_set_ChecksumCRC64NVME<Aws::S3::Model::PutObjectRequest>::value)result.push_back("checksum_crc64nvme");
    if constexpr(can_set_ChecksumSHA1<Aws::S3::Model::PutObjectRequest>::value)result.push_back("checksum_sha1");
    if constexpr(can_set_ChecksumSHA256<Aws::S3::Model::PutObjectRequest>::value)result.push_back("checksum_sha256");
    if constexpr(can_set_ChecksumType<Aws::S3::Model::CreateMultipartUploadRequest>::value)result.push_back("checksum_type");
    return result;
}
} // namespace osv_checksums
