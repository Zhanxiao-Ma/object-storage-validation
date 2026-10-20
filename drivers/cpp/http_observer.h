#pragma once
#include "object_metadata.h"
#include <aws/core/http/HttpClientFactory.h>
#include <aws/core/http/curl/CurlHttpClient.h>
#include <aws/core/http/standard/StandardHttpRequest.h>
#include <aws/core/http/HttpResponse.h>
#include <aws/core/http/URI.h>

inline thread_local nlohmann::json* osv_http_destination=nullptr;

/**
 * Capture status/headers even for empty service responses, where the SDK body callback never fires.
 * Delegate all transport, signing, retries and body handling to the official Curl client and inspect its real response.
 */
class ObservedHttpClient final : public Aws::Http::CurlHttpClient {
public:
    explicit ObservedHttpClient(const Aws::Client::ClientConfiguration& configuration):Aws::Http::CurlHttpClient(configuration) {}
    std::shared_ptr<Aws::Http::HttpResponse> MakeRequest(const std::shared_ptr<Aws::Http::HttpRequest>& request,
        Aws::Utils::RateLimits::RateLimiterInterface* readLimiter=nullptr,
        Aws::Utils::RateLimits::RateLimiterInterface* writeLimiter=nullptr) const override {
        auto response=Aws::Http::CurlHttpClient::MakeRequest(request,readLimiter,writeLimiter);
        if(osv_http_destination && response && static_cast<int>(response->GetResponseCode())>0) *osv_http_destination=http_observation(static_cast<int>(response->GetResponseCode()),response->GetHeaders());
        return response;
    }
};

/**
 * Install response observation using the public AWS transport factory, including zero-length bodies.
 * Keep the official Curl implementation and standard request/stream factories; SDK serialization and authentication remain authoritative.
 */
class ObservedHttpFactory final : public Aws::Http::HttpClientFactory {
public:
    std::shared_ptr<Aws::Http::HttpClient> CreateHttpClient(const Aws::Client::ClientConfiguration& configuration) const override {
        return Aws::MakeShared<ObservedHttpClient>("osv",configuration);
    }
    std::shared_ptr<Aws::Http::HttpRequest> CreateHttpRequest(const Aws::String& uri,Aws::Http::HttpMethod method,const Aws::IOStreamFactory& stream) const override {
        return CreateHttpRequest(Aws::Http::URI(uri),method,stream);
    }
    std::shared_ptr<Aws::Http::HttpRequest> CreateHttpRequest(const Aws::Http::URI& uri,Aws::Http::HttpMethod method,const Aws::IOStreamFactory& stream) const override {
        auto request=Aws::MakeShared<Aws::Http::Standard::StandardHttpRequest>("osv",uri,method);
        request->SetResponseStreamFactory(stream);return request;
    }
    void InitStaticState() override {Aws::Http::CurlHttpClient::InitGlobalState();}
    void CleanupStaticState() override {Aws::Http::CurlHttpClient::CleanupGlobalState();}
};

/**
 * Limit transport inspection to explicit compatibility operations, excluding ordinary performance requests.
 * Restore the previous thread-local destination on all success and exception paths to prevent stale response reuse.
 */
class HttpObservationScope final {
    nlohmann::json* previous;
public:
    explicit HttpObservationScope(nlohmann::json* destination):previous(osv_http_destination) {osv_http_destination=destination;}
    ~HttpObservationScope() {osv_http_destination=previous;}
    HttpObservationScope(const HttpObservationScope&)=delete;
    HttpObservationScope& operator=(const HttpObservationScope&)=delete;
};
