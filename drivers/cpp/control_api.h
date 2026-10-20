#pragma once
#include <aws/iam/IAMClient.h>
#include <aws/sts/STSClient.h>
#include <aws/core/auth/AWSCredentials.h>
#include <aws/core/client/DefaultRetryStrategy.h>
#include <aws/core/http/URI.h>
#include <aws/core/utils/StringUtils.h>
#include <map>
#include <mutex>
#include <limits>
#include <set>

namespace osv_control_api {
using J=nlohmann::json;
inline int64_t integer(const J& v,int64_t minimum,int64_t maximum) {
    if(!v.is_number_integer())throw std::runtime_error("SDK integer must be integral");
    if(v.is_number_unsigned()){auto n=v.get<uint64_t>();if(n>static_cast<uint64_t>(maximum))throw std::runtime_error("SDK integer outside range");return static_cast<int64_t>(n);}
    auto n=v.get<int64_t>();if(n<minimum || n>maximum)throw std::runtime_error("SDK integer outside range");return n;
}
inline J policy(const J& text) {
    // Keep the actual typed SDK string intact through JSON serialization. The
    // shared Python RPC consumer performs JSON/RFC3986 decoding, preserving its
    // original member order instead of sorting through nlohmann::json's map.
    if(!text.is_string())throw std::runtime_error("IAM policy document SDK value must be text");
    return text;
}
inline bool present(const J& observed,const std::string& wrapper,const std::string& field) {
    if(!observed.contains("result_fields") || !observed["result_fields"].contains(wrapper))return false;
    const auto& names=observed["result_fields"][wrapper];return std::find(names.begin(),names.end(),J(field))!=names.end();
}
inline std::string credential_prefix(const std::string& identity) {
    if(identity=="iam")return "OSV_IAM_";
    if(identity=="iam_root")return "OSV_IAM_ROOT_";
    if(identity=="iam_alt_root")return "OSV_IAM_ALT_ROOT_";
    throw std::runtime_error("unsupported control identity");
}
inline long positive(const char* name,long fallback) {
    auto raw=env(name,std::to_string(fallback));
    if(raw.empty() || raw.find_first_not_of("0123456789")!=std::string::npos)throw std::runtime_error(std::string(name)+" must be a positive integer");
    auto n=std::stol(raw);if(n<1 || n>2147483647L)throw std::runtime_error(std::string(name)+" outside 1..2147483647");return n;
}
inline Aws::Client::ClientConfiguration configuration(const std::string& service) {
    std::string prefix=service=="iam"?"OSV_IAM_":"OSV_STS_";
    auto endpoint=required((prefix+"ENDPOINT").c_str());
    Aws::Http::URI parsed(endpoint.c_str());
    if((endpoint.rfind("http://",0)!=0 && endpoint.rfind("https://",0)!=0) || parsed.GetAuthority().empty()
        || endpoint.find('@')!=std::string::npos || endpoint.find('?')!=std::string::npos || endpoint.find('#')!=std::string::npos)
        throw std::runtime_error("control endpoint must be explicit HTTP(S) without credentials/query/fragment");
    Aws::Client::ClientConfiguration c;c.endpointOverride=endpoint;c.region=required((prefix+"REGION").c_str());
    c.scheme=endpoint.rfind("http://",0)==0?Aws::Http::Scheme::HTTP:Aws::Http::Scheme::HTTPS;
    c.maxConnections=positive("OSV_CONNECTIONS",16);c.requestTimeoutMs=positive("OSV_REQUEST_TIMEOUT_MS",30000);
    c.httpRequestTimeoutMs=c.requestTimeoutMs;c.connectTimeoutMs=positive("OSV_CONNECT_TIMEOUT_MS",2000);
    c.retryStrategy=Aws::MakeShared<Aws::Client::DefaultRetryStrategy>("osv-control",std::stol(env("OSV_RETRIES","0")));return c;
}
inline Aws::Auth::AWSCredentials credentials(const std::string& identity) {
    auto p=credential_prefix(identity);
    return Aws::Auth::AWSCredentials(required((p+"ACCESS_KEY").c_str()),required((p+"SECRET_KEY").c_str()),env((p+"SESSION_TOKEN").c_str()));
}
inline std::map<std::string,std::shared_ptr<Aws::IAM::IAMClient>> iam_clients;
inline std::map<std::string,std::shared_ptr<Aws::STS::STSClient>> sts_clients;
inline std::mutex clients_mutex;
inline void close(){std::lock_guard<std::mutex> guard(clients_mutex);iam_clients.clear();sts_clients.clear();}
inline std::shared_ptr<Aws::IAM::IAMClient> iam_client(const std::string& identity) {
    std::lock_guard<std::mutex> guard(clients_mutex);
    auto found=iam_clients.find(identity);
    if(found==iam_clients.end())found=iam_clients.emplace(identity,std::make_shared<Aws::IAM::IAMClient>(credentials(identity),configuration("iam"))).first;
    return found->second;
}
inline std::shared_ptr<Aws::STS::STSClient> sts_client(const std::string& identity) {
    std::lock_guard<std::mutex> guard(clients_mutex);
    auto found=sts_clients.find(identity);
    if(found==sts_clients.end())found=sts_clients.emplace(identity,std::make_shared<Aws::STS::STSClient>(credentials(identity),configuration("sts"))).first;
    return found->second;
}
}
#include "control_iam_generated.h"
#include "control_sts_generated.h"
namespace osv_control_api {
inline J capabilities() {
    J result=J::array();
    for(const auto& service:{std::string("iam"),std::string("sts")}) {
        for(const auto& api:service=="iam"?osv_control_iam::operations():osv_control_sts::operations())result.push_back(api);
        std::string p=service=="iam"?"OSV_IAM_":"OSV_STS_";
        if(env((p+"ENDPOINT").c_str()).empty() || env((p+"REGION").c_str()).empty())continue;
        for(const auto& identity:{"iam","iam_root","iam_alt_root"}) {
            auto cp=credential_prefix(identity);
            if(!env((cp+"ACCESS_KEY").c_str()).empty() && !env((cp+"SECRET_KEY").c_str()).empty())result.push_back((service=="iam"?"identity_":"identity_sts_")+std::string(identity));
        }
    }
    return result;
}
inline bool handles(const std::string& op) {
    const auto& operations=op.rfind("api.iam.",0)==0?osv_control_iam::operations():osv_control_sts::operations();
    return std::find(operations.begin(),operations.end(),op)!=operations.end();
}
inline J execute(const J& q) {
    const std::string op=q.at("op");if(!handles(op))throw std::runtime_error("unsupported control operation");
    if(q.contains("wire_headers") || q.contains("wire_query") || q.contains("checksum_when_required"))throw std::runtime_error("S3 wire extensions are not control API fields");
    std::string identity=q.value("identity",std::string("iam"));credential_prefix(identity);
    if(op.rfind("api.iam.",0)==0)return osv_control_iam::execute(*iam_client(identity),q);
    return osv_control_sts::execute(*sts_client(identity),q);
}
}
