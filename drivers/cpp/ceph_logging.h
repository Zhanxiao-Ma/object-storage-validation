#pragma once
#include "ceph_logging_wire.h"
#include "bucket_api_generated.h"
#include <aws/core/utils/xml/XmlSerializer.h>
#include <aws/core/utils/HashingUtils.h>
#include <aws/s3/model/PutBucketLoggingRequest.h>
#include <aws/s3/model/GetBucketLoggingRequest.h>
#include <set>

namespace osv_ceph_logging {
using J=nlohmann::json;
inline const std::set<std::string> extensions={"LoggingType","ObjectRollTime","RecordsBatchSize","Filter","TargetObjectKeyFormat"};
inline void object(const J& value,const std::set<std::string>& keys) {
    if(!value.is_object())throw std::runtime_error("logging structure must be an object");
    for(auto it=value.begin();it!=value.end();++it)if(!keys.count(it.key()))throw std::runtime_error("unknown logging field: "+it.key());
}
inline void string(const J& value){if(!value.is_string())throw std::runtime_error("logging value must be a string");}
inline void validate(const J& e) {
    object(e,extensions);
    if(e.contains("LoggingType"))string(e.at("LoggingType"));
    for(const auto& k:{"ObjectRollTime","RecordsBatchSize"})if(e.contains(k)) {
        const auto& n=e.at(k);
        if(!n.is_number_integer() || (n.is_number_unsigned() && n.get<uint64_t>()>static_cast<uint64_t>(INT64_MAX)))throw std::runtime_error("logging integer must fit int64");
    }
    if(e.contains("Filter")) {
        const auto& f=e.at("Filter");object(f,{"Key"});
        if(f.contains("Key")) {const auto& key=f.at("Key");object(key,{"FilterRules"});if(key.contains("FilterRules")) {
            if(!key.at("FilterRules").is_array())throw std::runtime_error("FilterRules must be an array");
            for(const auto& rule:key.at("FilterRules")){object(rule,{"Name","Value"});for(const auto& k:{"Name","Value"})if(rule.contains(k))string(rule.at(k));}
        }}
    }
    if(e.contains("TargetObjectKeyFormat")) {
        const auto& f=e.at("TargetObjectKeyFormat");object(f,{"SimplePrefix","PartitionedPrefix"});
        if(f.contains("SimplePrefix"))object(f.at("SimplePrefix"),{});
        if(f.contains("PartitionedPrefix")){const auto& p=f.at("PartitionedPrefix");object(p,{"PartitionDateSource"});if(p.contains("PartitionDateSource"))string(p.at("PartitionDateSource"));}
    }
}
inline void append(Aws::Utils::Xml::XmlNode parent,const std::string& name,const J& value) {
    auto node=parent.CreateChildElement(name.c_str());
    if(value.is_primitive()){node.SetText((value.is_string()?value.get<std::string>():value.dump()).c_str());return;}
    for(auto it=value.begin();it!=value.end();++it) {
        if(it.key()=="FilterRules")for(const auto& rule:it.value())append(node,"FilterRule",rule);
        else append(node,it.key()=="Key"?"S3Key":it.key(),it.value());
    }
}
/**
 * Add only explicitly supported Ceph elements to the official SDK's serialized logging XML.
 * Inherit the typed request endpoint and headers so its normal checksum, signer and HTTP
 * producer consume the final virtual payload rather than a parallel hand-written request.
 */
class Request final:public Aws::S3::Model::PutBucketLoggingRequest {
    J extra;
public:
    Request(const Aws::S3::Model::PutBucketLoggingRequest& base,J extension):Aws::S3::Model::PutBucketLoggingRequest(base),extra(std::move(extension)){}
    Aws::String SerializePayload() const override {
        auto original=Aws::S3::Model::PutBucketLoggingRequest::SerializePayload();if(extra.empty())return original;
        // XmlDocument::ConvertToString always emits its own declaration, while
        // CreateFromXmlString retains the input declaration as a document node.
        // Remove only that leading declaration before the SDK parses the payload.
        auto first=original.find_first_not_of(" \t\r\n");
        if(first!=Aws::String::npos && original.compare(first,5,"<?xml")==0) {
            auto end=original.find("?>",first);
            if(end==Aws::String::npos)throw std::runtime_error("invalid official XML declaration");
            original.erase(0,end+2);
        }
        auto doc=Aws::Utils::Xml::XmlDocument::CreateFromXmlString(original);
        if(!doc.WasParseSuccessful())throw std::runtime_error("cannot parse official logging payload");
        auto enabled=doc.GetRootElement().FirstChild("LoggingEnabled");if(enabled.IsNull())throw std::runtime_error("SDK logging payload lacks LoggingEnabled");
        for(auto it=extra.begin();it!=extra.end();++it)append(enabled,it.key(),it.value());
        return doc.ConvertToString();
    }
    Aws::Http::HeaderValueCollection GetRequestSpecificHeaders() const override {
        auto headers=Aws::S3::Model::PutBucketLoggingRequest::GetRequestSpecificHeaders();
        if(!extra.empty())headers["content-md5"]=Aws::Utils::HashingUtils::Base64Encode(Aws::Utils::HashingUtils::CalculateMD5(SerializePayload()));
        return headers;
    }
};
/** Scope a genuine SDK GET model's HTTP method override to POST, restoring the prior method for cleanup even after service errors. */
class MethodScope final {
    bool previous;
public:
    explicit MethodScope(bool post):previous(osv_ceph_logging_post){osv_ceph_logging_post=post;}
    ~MethodScope(){osv_ceph_logging_post=previous;}
};
inline bool handles(const std::string& op){return op=="api.ceph.PutBucketLogging" || op=="api.ceph.GetBucketLogging" || op=="api.ceph.PostBucketLogging";}
inline J capabilities(){return J::array({"ceph_logging_wire_extensions"});}
inline J execute(Aws::S3::S3Client& client,const std::string& op,const J& q) {
    if(!handles(op))throw std::runtime_error("unsupported Ceph logging operation");
    if(!q.contains("input") || !q.at("input").is_object())throw std::runtime_error("api input must be an object");
    J input=q.at("input"),extra=J::object();
    if(q.contains("bucket")) {
        if(!q.at("bucket").is_string())throw std::runtime_error("bucket must be a string");
        if(input.contains("Bucket") && input.at("Bucket")!=q.at("bucket"))throw std::runtime_error("cannot override scoped API bucket");
        input["Bucket"]=q.at("bucket");
    }
    if(op=="api.ceph.PutBucketLogging" && input.contains("BucketLoggingStatus")) {
        auto& status=input["BucketLoggingStatus"];object(status,{"LoggingEnabled"});
        if(status.contains("LoggingEnabled")) {
            auto& e=status["LoggingEnabled"];object(e,{"TargetBucket","TargetPrefix","TargetGrants","LoggingType","ObjectRollTime","RecordsBatchSize","Filter","TargetObjectKeyFormat"});
            for(const auto& k:extensions)if(e.contains(k)){extra[k]=e[k];e.erase(k);}
        }
    }
    validate(extra);
    J observed;HttpObservationScope observation(&observed,true);
    MethodScope method(op=="api.ceph.PostBucketLogging");
    if(op=="api.ceph.PutBucketLogging") {
        auto base=osv_bucket_api::read_PutBucketLoggingRequest(input);Request request(base,extra);
        auto outcome=client.PutBucketLogging(request);checked(outcome);osv_response_body::metadata(outcome.GetResult());
    } else {
        auto request=osv_bucket_api::read_GetBucketLoggingRequest(input);
        auto outcome=client.GetBucketLogging(request);checked(outcome);osv_response_body::metadata(outcome.GetResult());
    }
    if(!observed.contains("status_code"))throw std::runtime_error("SDK HTTP observation unavailable");
    observed["data"]=J::object();return observed;
}
}
