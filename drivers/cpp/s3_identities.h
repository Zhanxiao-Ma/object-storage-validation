#pragma once
#include <map>

/**
 * Runs authorization scenarios with distinct real SDK credential contexts.
 * Cache isolated clients using identical transport settings and explicit credentials;
 * the official signer skips anonymous empty credentials without consulting the main identity.
 * Environment secrets never appear in capability metadata or error values.
 */
class S3Identities {
    std::map<std::string,std::shared_ptr<Aws::S3::S3Client>> clients;
    static std::string prefix(const std::string& name) {
        if(name=="main")return "";
        if(name=="alt")return "ALT_";
        if(name=="tenant")return "TENANT_";
        if(name=="bad_auth")return "BAD_AUTH_";
        if(name=="anonymous")return "";
        throw std::runtime_error("unsupported identity");
    }
    static std::shared_ptr<Aws::S3::S3Client> create(const std::string& name,bool headers,bool required_checksum) {
        auto credential_prefix=prefix(name);
        Aws::Client::ClientConfiguration configuration;
        configuration.endpointOverride = required("OSV_ENDPOINT");
        configuration.region = required("OSV_REGION");
        configuration.scheme = configuration.endpointOverride.find("http://") == 0
            ? Aws::Http::Scheme::HTTP : Aws::Http::Scheme::HTTPS;
        configuration.maxConnections = std::stoul(env("OSV_CONNECTIONS", "16"));
        if(required_checksum)configuration.checksumConfig.requestChecksumCalculation=Aws::Client::RequestChecksumCalculation::WHEN_REQUIRED;
        configuration.requestTimeoutMs = headers?std::stoul(env("OSV_HEADER_REQUEST_TIMEOUT_MS","30000")):30000;
        if(headers)configuration.httpRequestTimeoutMs=configuration.requestTimeoutMs;
        configuration.connectTimeoutMs = 10000;
        configuration.retryStrategy = Aws::MakeShared<Aws::Client::DefaultRetryStrategy>(
            "osv", std::stol(env("OSV_RETRIES", "0")));
        Aws::Auth::AWSCredentials credentials;
        if(name!="anonymous")credentials=Aws::Auth::AWSCredentials(
            required(("OSV_"+credential_prefix+"ACCESS_KEY").c_str()),
            required(("OSV_"+credential_prefix+"SECRET_KEY").c_str()),
            env(("OSV_"+credential_prefix+"SESSION_TOKEN").c_str()));
        if(headers)return std::make_shared<HeaderS3Client>(credentials,configuration,
            Aws::Client::AWSAuthV4Signer::PayloadSigningPolicy::Never,env("OSV_PATH_STYLE","false")!="true");
        return std::make_shared<Aws::S3::S3Client>(credentials,configuration,
            Aws::Client::AWSAuthV4Signer::PayloadSigningPolicy::Never,env("OSV_PATH_STYLE","false")!="true");
    }
public:
    static json capabilities() {
        json result=json::array({"identity_main","identity_anonymous","wire_headers","checksum_when_required"});
        for(const auto& name:{"alt","tenant","bad_auth"}) {
            auto credential_prefix=prefix(name);
            if(!env(("OSV_"+credential_prefix+"ACCESS_KEY").c_str()).empty() && !env(("OSV_"+credential_prefix+"SECRET_KEY").c_str()).empty())result.push_back(std::string("identity_")+name);
        }
        return result;
    }
    std::shared_ptr<Aws::S3::S3Client> get(const std::string& name,bool headers=false,bool required_checksum=false) {
        prefix(name);
        auto key=name+(headers?":wire":"")+(required_checksum?":required-checksum":"");
        auto found=clients.find(key);
        if(found!=clients.end())return found->second;
        auto result=create(name,headers,required_checksum);clients.emplace(key,result);return result;
    }
    std::shared_ptr<Aws::S3::S3Client> select(const json& request) {
        bool required_checksum=false;
        if(request.contains("checksum_when_required")) {
            if(!request.at("checksum_when_required").is_boolean() || !request.at("checksum_when_required").get<bool>())throw std::runtime_error("checksum_when_required must be true");
            required_checksum=true;
        }
        if(!request.contains("identity"))return get("main",request.contains("wire_headers"),required_checksum);
        if(!request.at("identity").is_string())throw std::runtime_error("identity must be a string");
        return get(request.at("identity").get<std::string>(),request.contains("wire_headers"),required_checksum);
    }
};
