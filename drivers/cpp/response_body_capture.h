#pragma once
#include <nlohmann/json.hpp>
#include <aws/core/http/HttpResponse.h>
#include <aws/core/utils/HashingUtils.h>
#include <type_traits>
#include <memory>
#include <string>
#include <vector>

namespace osv_response_body {
/** Store actual bytes and typed metadata for one explicitly requested RPC; keep thread-local state so parallel workers never exchange response bodies. */
struct State { std::string body,request_id,error;bool has_body=false,has_request_id=false; };
inline thread_local std::unique_ptr<State> active;
inline void reset(){active.reset();}
inline void begin(const nlohmann::json& request) {
    if(!request.contains("capture_response_body"))return;
    if(!request.at("capture_response_body").is_boolean())throw std::runtime_error("capture_response_body must be boolean");
    if(request.at("capture_response_body").get<bool>()) {
        if(request.value("op",std::string())=="concurrent_requests")throw std::runtime_error("body capture requires an individual SDK request");
        active=std::make_unique<State>();
    }
}
inline void capture(const std::shared_ptr<Aws::Http::HttpResponse>& response) {
    if(!active || !response)return;
    active->body.clear();active->request_id.clear();active->error.clear();active->has_body=false;active->has_request_id=false;
    auto& stream=response->GetResponseBody();auto previous=stream.rdstate();auto position=stream.tellg();
    if(position==std::streampos(-1)){stream.clear(previous);active->error="actual response body is not seekable";return;}
    constexpr size_t limit=512*1024;std::vector<char> bytes(limit+1);
    stream.read(bytes.data(),static_cast<std::streamsize>(bytes.size()));auto count=stream.gcount();
    stream.clear();stream.seekg(position);
    if(!stream){active->error="cannot restore actual SDK response stream";throw std::runtime_error(active->error);}
    stream.clear(previous);
    if(count>static_cast<std::streamsize>(limit)){active->error="actual response exceeds 512 KiB capture limit";return;}
    active->body.assign(bytes.data(),static_cast<size_t>(count));active->has_body=true;
}
/** Detect genuine model metadata getters at compile time; absent SDK metadata stays absent rather than being reconstructed from XML or headers. */
template<class T,class=void> struct HasRequestId : std::false_type {};
/** Select official result/error types that actually expose a request ID; never infer one from the independently captured raw body. */
template<class T> struct HasRequestId<T,std::void_t<decltype(std::declval<const T&>().GetRequestId())>> : std::true_type {};
template<class T> inline void metadata(const T& result) {
    if(!active)return;
    if constexpr(HasRequestId<T>::value){active->request_id=result.GetRequestId().c_str();active->has_request_id=true;}
}
inline void attach(nlohmann::json& result) {
    if(!active)return;
    if(active->has_body) {
        Aws::Utils::ByteBuffer bytes(reinterpret_cast<const unsigned char*>(active->body.data()),active->body.size());
        result["response_body_b64"]=std::string(Aws::Utils::HashingUtils::Base64Encode(bytes).c_str());
    }
    if(active->has_request_id)result["request_id"]=active->request_id;
    if(!active->error.empty())result["response_body_capture_error"]=active->error;
    reset();
}
}
