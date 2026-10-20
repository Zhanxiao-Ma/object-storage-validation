#pragma once
#include "object_metadata.h"
#include "response_body_capture.h"
#include "ceph_logging_wire.h"
#include <aws/core/http/HttpClientFactory.h>
#include <aws/core/http/curl/CurlHttpClient.h>
#include <aws/core/http/standard/StandardHttpRequest.h>
#include <aws/core/http/HttpResponse.h>
#include <aws/core/http/URI.h>
#include <aws/core/utils/xml/XmlSerializer.h>
#include <set>

inline thread_local nlohmann::json* osv_http_destination=nullptr;
inline thread_local bool osv_http_capture_xml_fields=false;

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
        osv_response_body::capture(response);
        if(osv_http_destination && response && static_cast<int>(response->GetResponseCode())>0) {
            *osv_http_destination=http_observation(static_cast<int>(response->GetResponseCode()),response->GetHeaders());
            if(osv_http_capture_xml_fields) {
                auto& body=response->GetResponseBody();auto position=body.tellg();
                if(position==std::streampos(-1)) throw std::runtime_error("cannot observe non-seekable XML response");
                auto document=Aws::Utils::Xml::XmlDocument::CreateFromXmlStream(body);
                body.clear();body.seekg(position);
                if(!body) throw std::runtime_error("cannot restore SDK XML response stream");
                std::set<std::string> names;
                auto root=document.GetRootElement();
                if(document.WasParseSuccessful() && !root.IsNull()) {
                    (*osv_http_destination)["xml_root"]=std::string(root.GetName().c_str());
                    for(auto node=root.FirstChild();!node.IsNull();node=node.NextNode()) {
                        names.emplace(node.GetName().c_str());
                        // Query services wrap modeled output in OperationResult. Observe
                        // genuine child presence before the official SDK consumes the body.
                        std::string name=node.GetName().c_str();
                        if(name.size()>=6 && name.compare(name.size()-6,6,"Result")==0) {
                            std::set<std::string> members;
                            for(auto member=node.FirstChild();!member.IsNull();member=member.NextNode())members.emplace(member.GetName().c_str());
                            (*osv_http_destination)["result_fields"][name]=members;
                        }
                    }
                } else (*osv_http_destination)["xml_root"]=nullptr;
                (*osv_http_destination)["fields"]=names;
            }
        }

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
        method=osv_ceph_logging_post?Aws::Http::HttpMethod::HTTP_POST:method;
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
    bool previous_xml;
public:
    explicit HttpObservationScope(nlohmann::json* destination,bool capture_xml=false):previous(osv_http_destination),previous_xml(osv_http_capture_xml_fields) {osv_http_destination=destination;osv_http_capture_xml_fields=capture_xml;}
    ~HttpObservationScope() {osv_http_destination=previous;osv_http_capture_xml_fields=previous_xml;}
    HttpObservationScope(const HttpObservationScope&)=delete;
    HttpObservationScope& operator=(const HttpObservationScope&)=delete;
};
