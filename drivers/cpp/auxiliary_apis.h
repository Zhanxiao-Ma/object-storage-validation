#pragma once
#include <aws/sns/SNSClient.h>
#include <aws/s3control/S3ControlClient.h>
#include <aws/core/auth/AWSCredentials.h>
#include <aws/core/client/DefaultRetryStrategy.h>
#include <aws/core/http/URI.h>
#include <set>
#include <map>
#include <mutex>
#include <aws/sns/model/CreateTopicRequest.h>
#include <aws/sns/model/ListTopicsRequest.h>
#include <aws/sns/model/SetTopicAttributesRequest.h>
#include <aws/sns/model/GetTopicAttributesRequest.h>
#include <aws/sns/model/DeleteTopicRequest.h>
#include <aws/s3control/model/GetPublicAccessBlockRequest.h>
#include <aws/s3control/model/PutPublicAccessBlockRequest.h>
#include <aws/s3control/model/DeletePublicAccessBlockRequest.h>
#include <aws/s3control/model/PublicAccessBlockConfiguration.h>

namespace osv_auxiliary {
using J=nlohmann::json;
inline const std::map<std::string,std::vector<std::string>> operations={
    {"sns",{"CreateTopic","ListTopics","SetTopicAttributes","GetTopicAttributes","DeleteTopic"}},
    {"s3control",{"GetPublicAccessBlock","PutPublicAccessBlock","DeletePublicAccessBlock"}}};
inline void object(const J& input,const std::set<std::string>& keys) {
    if(!input.is_object())throw std::runtime_error("SDK input must be an object");
    for(auto it=input.begin();it!=input.end();++it)if(!keys.count(it.key()))throw std::runtime_error("SDK model field not supported by this typed subset: "+it.key());
}
inline Aws::String text(const J& input,const std::string& name) {
    if(!input.contains(name) || !input.at(name).is_string())throw std::runtime_error("SDK text field required: "+name);
    return input.at(name).get<std::string>().c_str();
}
inline std::string prefix(const std::string& identity) {
    if(identity=="main")return "OSV_";
    if(identity=="iam_root")return "OSV_IAM_ROOT_";
    if(identity=="iam_alt_root")return "OSV_IAM_ALT_ROOT_";
    throw std::runtime_error("unsupported auxiliary identity");
}
inline Aws::Auth::AWSCredentials credentials(const std::string& identity) {
    auto p=prefix(identity);return Aws::Auth::AWSCredentials(required((p+"ACCESS_KEY").c_str()),required((p+"SECRET_KEY").c_str()),env((p+"SESSION_TOKEN").c_str()));
}
inline long positive(const char* name,long fallback) {
    auto s=env(name,std::to_string(fallback));if(s.empty() || s.find_first_not_of("0123456789")!=std::string::npos)throw std::runtime_error("SDK timeout/connections must be a positive integer");
    auto n=std::stol(s);if(n<1 || n>2147483647L)throw std::runtime_error("SDK timeout/connections outside int32 range");return n;
}
inline Aws::Client::ClientConfiguration configuration(const std::string& service) {
    std::string p=service=="sns"?"OSV_SNS_":"OSV_S3CONTROL_";auto endpoint=required((p+"ENDPOINT").c_str());Aws::Http::URI uri(endpoint.c_str());
    if((endpoint.rfind("http://",0)!=0 && endpoint.rfind("https://",0)!=0) || uri.GetAuthority().empty() || endpoint.find('@')!=std::string::npos || endpoint.find('?')!=std::string::npos || endpoint.find('#')!=std::string::npos)throw std::runtime_error("auxiliary endpoint must be explicit HTTP(S) without credentials/query/fragment");
    Aws::Client::ClientConfiguration c;c.endpointOverride=endpoint;c.region=required((p+"REGION").c_str());c.scheme=endpoint.rfind("http://",0)==0?Aws::Http::Scheme::HTTP:Aws::Http::Scheme::HTTPS;
    c.maxConnections=positive("OSV_CONNECTIONS",16);c.requestTimeoutMs=positive("OSV_REQUEST_TIMEOUT_MS",30000);c.httpRequestTimeoutMs=c.requestTimeoutMs;c.connectTimeoutMs=positive("OSV_CONNECT_TIMEOUT_MS",2000);
    c.retryStrategy=Aws::MakeShared<Aws::Client::DefaultRetryStrategy>("osv-aux",std::stol(env("OSV_RETRIES","0")));return c;
}
/**
 * Keep official SNS/S3Control clients separate from S3 and from each credential identity.
 * Cache native typed clients behind a mutex, preserving official endpoint resolution,
 * serialization, signing, transport and service error parsing for each real request.
 */
class Clients final {
    std::mutex mutex;
    std::map<std::string,std::shared_ptr<Aws::SNS::SNSClient>> sns;
    std::map<std::string,std::shared_ptr<Aws::S3Control::S3ControlClient>> control;
public:
    std::shared_ptr<Aws::SNS::SNSClient> topic(const std::string& identity) {
        std::lock_guard<std::mutex> guard(mutex);auto found=sns.find(identity);
        if(found==sns.end())found=sns.emplace(identity,std::make_shared<Aws::SNS::SNSClient>(credentials(identity),configuration("sns"))).first;return found->second;
    }
    std::shared_ptr<Aws::S3Control::S3ControlClient> account(const std::string& identity) {
        std::lock_guard<std::mutex> guard(mutex);auto found=control.find(identity);
        if(found==control.end())found=control.emplace(identity,std::make_shared<Aws::S3Control::S3ControlClient>(credentials(identity),configuration("s3control"))).first;return found->second;
    }
    void close(){std::lock_guard<std::mutex> guard(mutex);sns.clear();control.clear();}
};
inline Clients clients;
inline bool handles(const std::string& op){for(const auto& service:operations)for(const auto& name:service.second)if(op=="api."+service.first+"."+name)return true;return false;}
inline J capabilities() {
    J result=J::array();for(const auto& service:operations) {
        for(const auto& name:service.second)result.push_back("api."+service.first+"."+name);
        std::string p=service.first=="sns"?"OSV_SNS_":"OSV_S3CONTROL_";
        if(env((p+"ENDPOINT").c_str()).empty() || env((p+"REGION").c_str()).empty())continue;
        for(const auto& identity:{"main","iam_root","iam_alt_root"}) {
            auto c=prefix(identity);if(!env((c+"ACCESS_KEY").c_str()).empty() && !env((c+"SECRET_KEY").c_str()).empty())result.push_back("identity_"+service.first+"_"+identity);
        }
    }return result;
}
inline bool present(const J& observed,const std::string& wrapper,const std::string& field) {
    if(!observed.contains("result_fields") || !observed["result_fields"].contains(wrapper))return false;
    const auto& names=observed["result_fields"][wrapper];return std::find(names.begin(),names.end(),J(field))!=names.end();
}
inline J execute(const J& q) {
    std::string op=q.at("op");if(!handles(op))throw std::runtime_error("unsupported auxiliary SDK operation");
    if(q.contains("wire_headers") || q.contains("wire_query") || q.contains("checksum_when_required"))throw std::runtime_error("S3 wire fields are not auxiliary service parameters");
    std::string identity=q.contains("identity")?text(q,"identity").c_str():"iam_root";prefix(identity);
    const auto& input=q.at("input");J observed,data=J::object();HttpObservationScope observation(&observed,true);
    if(op=="api.sns.CreateTopic") {
        object(input,{"Name"});Aws::SNS::Model::CreateTopicRequest request;
        if(input.contains("Name"))request.SetName(text(input,"Name"));
        auto outcome=clients.topic(identity)->CreateTopic(request);checked(outcome);osv_response_body::metadata(outcome.GetResult());
        if(present(observed,"CreateTopicResult","TopicArn"))data["TopicArn"]=std::string(outcome.GetResult().GetTopicArn().c_str());
    }
    if(op=="api.sns.ListTopics") {
        object(input,{"NextToken"});Aws::SNS::Model::ListTopicsRequest request;
        if(input.contains("NextToken"))request.SetNextToken(text(input,"NextToken"));
        auto outcome=clients.topic(identity)->ListTopics(request);checked(outcome);osv_response_body::metadata(outcome.GetResult());
        if(present(observed,"ListTopicsResult","Topics")){data["Topics"]=J::array();for(const auto& topic:outcome.GetResult().GetTopics()) {J row=J::object();if(topic.TopicArnHasBeenSet())row["TopicArn"]=std::string(topic.GetTopicArn().c_str());data["Topics"].push_back(row);}}
        if(present(observed,"ListTopicsResult","NextToken"))data["NextToken"]=std::string(outcome.GetResult().GetNextToken().c_str());
    }
    if(op=="api.sns.GetTopicAttributes") {
        object(input,{"TopicArn"});Aws::SNS::Model::GetTopicAttributesRequest request;
        if(input.contains("TopicArn"))request.SetTopicArn(text(input,"TopicArn"));
        auto outcome=clients.topic(identity)->GetTopicAttributes(request);checked(outcome);osv_response_body::metadata(outcome.GetResult());
        if(present(observed,"GetTopicAttributesResult","Attributes")){data["Attributes"]=J::object();for(const auto& attr:outcome.GetResult().GetAttributes())data["Attributes"][std::string(attr.first.c_str())]=std::string(attr.second.c_str());}
    }
    if(op=="api.sns.SetTopicAttributes") {
        object(input,{"TopicArn","AttributeName","AttributeValue"});Aws::SNS::Model::SetTopicAttributesRequest request;
        if(input.contains("TopicArn"))request.SetTopicArn(text(input,"TopicArn"));
        if(input.contains("AttributeName"))request.SetAttributeName(text(input,"AttributeName"));
        if(input.contains("AttributeValue"))request.SetAttributeValue(text(input,"AttributeValue"));
        auto outcome=clients.topic(identity)->SetTopicAttributes(request);checked(outcome);osv_response_body::metadata(outcome.GetResult());
    }
    if(op=="api.sns.DeleteTopic") {
        object(input,{"TopicArn"});Aws::SNS::Model::DeleteTopicRequest request;
        if(input.contains("TopicArn"))request.SetTopicArn(text(input,"TopicArn"));
        auto outcome=clients.topic(identity)->DeleteTopic(request);checked(outcome);osv_response_body::metadata(outcome.GetResult());
    }
    if(op=="api.s3control.GetPublicAccessBlock") {
        object(input,{"AccountId"});Aws::S3Control::Model::GetPublicAccessBlockRequest request;request.SetAccountId(text(input,"AccountId"));
        auto outcome=clients.account(identity)->GetPublicAccessBlock(request);checked(outcome);osv_response_body::metadata(outcome.GetResult());
        if(observed.value("xml_root",J())==J("PublicAccessBlockConfiguration")) {
            J config=J::object();const auto& value=outcome.GetResult().GetPublicAccessBlockConfiguration();
            if(value.BlockPublicAclsHasBeenSet())config["BlockPublicAcls"]=value.GetBlockPublicAcls();
            if(value.IgnorePublicAclsHasBeenSet())config["IgnorePublicAcls"]=value.GetIgnorePublicAcls();
            if(value.BlockPublicPolicyHasBeenSet())config["BlockPublicPolicy"]=value.GetBlockPublicPolicy();
            if(value.RestrictPublicBucketsHasBeenSet())config["RestrictPublicBuckets"]=value.GetRestrictPublicBuckets();
            data["PublicAccessBlockConfiguration"]=config;
        }
    }
    if(op=="api.s3control.PutPublicAccessBlock") {
        object(input,{"AccountId","PublicAccessBlockConfiguration"});Aws::S3Control::Model::PutPublicAccessBlockRequest request;request.SetAccountId(text(input,"AccountId"));
        const auto& value=input.at("PublicAccessBlockConfiguration");object(value,{"BlockPublicAcls","IgnorePublicAcls","BlockPublicPolicy","RestrictPublicBuckets"});Aws::S3Control::Model::PublicAccessBlockConfiguration config;
        if(value.contains("BlockPublicAcls")){if(!value.at("BlockPublicAcls").is_boolean())throw std::runtime_error("SDK bool required: BlockPublicAcls");config.SetBlockPublicAcls(value.at("BlockPublicAcls").get<bool>());}
        if(value.contains("IgnorePublicAcls")){if(!value.at("IgnorePublicAcls").is_boolean())throw std::runtime_error("SDK bool required: IgnorePublicAcls");config.SetIgnorePublicAcls(value.at("IgnorePublicAcls").get<bool>());}
        if(value.contains("BlockPublicPolicy")){if(!value.at("BlockPublicPolicy").is_boolean())throw std::runtime_error("SDK bool required: BlockPublicPolicy");config.SetBlockPublicPolicy(value.at("BlockPublicPolicy").get<bool>());}
        if(value.contains("RestrictPublicBuckets")){if(!value.at("RestrictPublicBuckets").is_boolean())throw std::runtime_error("SDK bool required: RestrictPublicBuckets");config.SetRestrictPublicBuckets(value.at("RestrictPublicBuckets").get<bool>());}
        request.SetPublicAccessBlockConfiguration(config);
        auto outcome=clients.account(identity)->PutPublicAccessBlock(request);checked(outcome);osv_response_body::metadata(outcome.GetResult());
    }
    if(op=="api.s3control.DeletePublicAccessBlock") {
        object(input,{"AccountId"});Aws::S3Control::Model::DeletePublicAccessBlockRequest request;request.SetAccountId(text(input,"AccountId"));
        auto outcome=clients.account(identity)->DeletePublicAccessBlock(request);checked(outcome);osv_response_body::metadata(outcome.GetResult());
    }
    if(!observed.contains("status_code"))throw std::runtime_error("SDK HTTP observation unavailable");
    observed["data"]=data;observed.erase("fields");observed.erase("result_fields");observed.erase("xml_root");return observed;
}
inline void close(){clients.close();}
}
