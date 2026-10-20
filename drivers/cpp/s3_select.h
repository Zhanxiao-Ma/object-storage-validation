#pragma once
#include <aws/s3/model/SelectObjectContentRequest.h>
#include <aws/s3/model/SelectObjectContentHandler.h>
#include <aws/s3/model/CSVInput.h>
#include <aws/s3/model/CSVOutput.h>
#include <aws/s3/model/JSONInput.h>
#include <aws/s3/model/JSONOutput.h>
#include <aws/s3/model/InputSerialization.h>
#include <aws/s3/model/OutputSerialization.h>
#include <aws/s3/model/RequestProgress.h>
#include <aws/s3/model/ScanRange.h>
#include <fstream>
#include "http_observer.h"

/**
 * Preserve actual official S3 Select event decoding without replacing it with a
 * local event parser or SQL engine. Typed request setters retain present fields;
 * native Records callbacks write opaque bytes and exact callback boundaries,
 * while stream errors remain iterator events after a successful HTTP response.
 */
namespace osv_select {
inline void fields(const json& value,std::initializer_list<const char*> allowed) {
    if(!value.is_object())throw std::runtime_error("Select input must be an object");
    for(auto it=value.begin();it!=value.end();++it){bool found=false;for(auto name:allowed)if(it.key()==name)found=true;if(!found)throw std::runtime_error("Unknown Select field: "+it.key());}
}
inline Aws::S3::Model::CSVInput readCSVInput(const json& q) {
fields(q,{"Comments","QuoteEscapeCharacter","RecordDelimiter","FieldDelimiter","QuoteCharacter","FileHeaderInfo","AllowQuotedRecordDelimiter"});Aws::S3::Model::CSVInput value;
if(q.contains("Comments"))value.SetComments(q.at("Comments").get<std::string>().c_str());
if(q.contains("QuoteEscapeCharacter"))value.SetQuoteEscapeCharacter(q.at("QuoteEscapeCharacter").get<std::string>().c_str());
if(q.contains("RecordDelimiter"))value.SetRecordDelimiter(q.at("RecordDelimiter").get<std::string>().c_str());
if(q.contains("FieldDelimiter"))value.SetFieldDelimiter(q.at("FieldDelimiter").get<std::string>().c_str());
if(q.contains("QuoteCharacter"))value.SetQuoteCharacter(q.at("QuoteCharacter").get<std::string>().c_str());
if(q.contains("FileHeaderInfo"))value.SetFileHeaderInfo(Aws::S3::Model::FileHeaderInfoMapper::GetFileHeaderInfoForName(q.at("FileHeaderInfo").get<std::string>().c_str()));
if(q.contains("AllowQuotedRecordDelimiter"))value.SetAllowQuotedRecordDelimiter(q.at("AllowQuotedRecordDelimiter").get<bool>());
return value;}
inline Aws::S3::Model::CSVOutput readCSVOutput(const json& q) {
fields(q,{"QuoteEscapeCharacter","RecordDelimiter","FieldDelimiter","QuoteCharacter","QuoteFields"});Aws::S3::Model::CSVOutput value;
if(q.contains("QuoteEscapeCharacter"))value.SetQuoteEscapeCharacter(q.at("QuoteEscapeCharacter").get<std::string>().c_str());
if(q.contains("RecordDelimiter"))value.SetRecordDelimiter(q.at("RecordDelimiter").get<std::string>().c_str());
if(q.contains("FieldDelimiter"))value.SetFieldDelimiter(q.at("FieldDelimiter").get<std::string>().c_str());
if(q.contains("QuoteCharacter"))value.SetQuoteCharacter(q.at("QuoteCharacter").get<std::string>().c_str());
if(q.contains("QuoteFields"))value.SetQuoteFields(Aws::S3::Model::QuoteFieldsMapper::GetQuoteFieldsForName(q.at("QuoteFields").get<std::string>().c_str()));
return value;}
inline Aws::S3::Model::JSONInput readJSONInput(const json& q) {
fields(q,{"Type"});Aws::S3::Model::JSONInput value;
if(q.contains("Type"))value.SetType(Aws::S3::Model::JSONTypeMapper::GetJSONTypeForName(q.at("Type").get<std::string>().c_str()));
return value;}
inline Aws::S3::Model::JSONOutput readJSONOutput(const json& q) {
fields(q,{"RecordDelimiter"});Aws::S3::Model::JSONOutput value;
if(q.contains("RecordDelimiter"))value.SetRecordDelimiter(q.at("RecordDelimiter").get<std::string>().c_str());
return value;}
inline json execute(Aws::S3::S3Client& client,const json& q) {
    using namespace Aws::S3::Model;
    const auto& input=q.at("input");
    fields(input,{"Bucket","Key","Expression","ExpressionType","InputSerialization","OutputSerialization","RequestProgress","ScanRange","SSECustomerAlgorithm","SSECustomerKey","SSECustomerKeyMD5","ExpectedBucketOwner"});
    SelectObjectContentRequest request;
if(input.contains("Bucket"))request.SetBucket(input.at("Bucket").get<std::string>().c_str());
if(input.contains("Key"))request.SetKey(input.at("Key").get<std::string>().c_str());
if(input.contains("Expression"))request.SetExpression(input.at("Expression").get<std::string>().c_str());
if(input.contains("SSECustomerAlgorithm"))request.SetSSECustomerAlgorithm(input.at("SSECustomerAlgorithm").get<std::string>().c_str());
if(input.contains("SSECustomerKey"))request.SetSSECustomerKey(input.at("SSECustomerKey").get<std::string>().c_str());
if(input.contains("SSECustomerKeyMD5"))request.SetSSECustomerKeyMD5(input.at("SSECustomerKeyMD5").get<std::string>().c_str());
if(input.contains("ExpectedBucketOwner"))request.SetExpectedBucketOwner(input.at("ExpectedBucketOwner").get<std::string>().c_str());
    if(input.contains("ExpressionType"))request.SetExpressionType(ExpressionTypeMapper::GetExpressionTypeForName(input.at("ExpressionType").get<std::string>().c_str()));
    if(input.contains("InputSerialization")){
        const auto& v=input.at("InputSerialization");fields(v,{"CSV","JSON","Parquet","CompressionType"});InputSerialization model;
        if(v.contains("CSV"))model.SetCSV(readCSVInput(v.at("CSV")));
        if(v.contains("JSON"))model.SetJSON(readJSONInput(v.at("JSON")));
        if(v.contains("Parquet")){fields(v.at("Parquet"),{});model.SetParquet(ParquetInput());}
        if(v.contains("CompressionType"))model.SetCompressionType(CompressionTypeMapper::GetCompressionTypeForName(v.at("CompressionType").get<std::string>().c_str()));
        request.SetInputSerialization(model);
    }
    if(input.contains("OutputSerialization")){
        const auto& v=input.at("OutputSerialization");fields(v,{"CSV","JSON"});OutputSerialization model;
        if(v.contains("CSV"))model.SetCSV(readCSVOutput(v.at("CSV")));
        if(v.contains("JSON"))model.SetJSON(readJSONOutput(v.at("JSON")));
        request.SetOutputSerialization(model);
    }
    if(input.contains("RequestProgress")){const auto& v=input.at("RequestProgress");fields(v,{"Enabled"});RequestProgress model;if(v.contains("Enabled"))model.SetEnabled(v.at("Enabled").get<bool>());request.SetRequestProgress(model);}
    if(input.contains("ScanRange")){const auto& v=input.at("ScanRange");fields(v,{"Start","End"});ScanRange model;if(v.contains("Start"))model.SetStart(v.at("Start").get<long long>());if(v.contains("End"))model.SetEnd(v.at("End").get<long long>());request.SetScanRange(model);}
    std::ofstream output(q.at("output_path").get<std::string>(),std::ios::binary|std::ios::trunc);
    if(!output)throw std::runtime_error("Cannot open actual Select Records output");
    json events=json::array(),observed=json::object();long long offset=0;
    SelectObjectContentHandler handler;
    handler.SetRecordsEventCallback([&](const RecordsEvent& event){const auto& bytes=event.GetPayload();auto size=bytes.size();output.write(reinterpret_cast<const char*>(bytes.data()),size);if(!output)throw std::runtime_error("Cannot write actual Select Records");events.push_back({{"type","Records"},{"offset",offset},{"length",size}});offset+=size;});
    auto details=[](const auto& d){return json{{"BytesScanned",d.GetBytesScanned()},{"BytesProcessed",d.GetBytesProcessed()},{"BytesReturned",d.GetBytesReturned()}};};
    handler.SetStatsEventCallback([&](const StatsEvent& event){events.push_back({{"type","Stats"},{"Details",details(event.GetDetails())}});});
    handler.SetProgressEventCallback([&](const ProgressEvent& event){events.push_back({{"type","Progress"},{"Details",details(event.GetDetails())}});});
    handler.SetContinuationEventCallback([&](){events.push_back({{"type","Cont"}});});
    handler.SetEndEventCallback([&](){events.push_back({{"type","End"}});});
    handler.SetOnErrorCallback([&](const Aws::Client::AWSError<Aws::S3::S3Errors>& error){events.push_back({{"type","Error"},{"error_kind",static_cast<bool>(request.GetEventStreamHandler())?"service":"decoder"},{"error_code",std::string(error.GetExceptionName().c_str())},{"error_message",std::string(error.GetMessage().c_str())},{"status_code",static_cast<int>(error.GetResponseCode())}});});
    request.SetEventStreamHandler(handler);
    HttpObservationScope observation(&observed);
    auto outcome=client.SelectObjectContent(request);
    if(!outcome.IsSuccess() && observed.value("status_code",0)!=200)checked(outcome);
    if(!outcome.IsSuccess() && (events.empty() || events.back().value("type","")!="Error")){
        const auto& error=outcome.GetError();events.push_back({{"type","Error"},{"error_kind","transport"},{"error_code",std::string(error.GetExceptionName().c_str())},{"error_message",std::string(error.GetMessage().c_str())},{"status_code",static_cast<int>(error.GetResponseCode())}});
    }
    output.flush();if(!output)throw std::runtime_error("Cannot flush actual Select Records");
    return {{"events",events},{"records_size",offset},{"status_code",observed.value("status_code",0)},{"http_headers",observed.value("http_headers",json::object())}};
}
}
