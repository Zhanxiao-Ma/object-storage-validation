#pragma once
#include <aws/s3/S3Client.h>
#include <aws/s3/model/GetObjectRequest.h>
#include <aws/s3/model/PutObjectRequest.h>
#include <aws/s3/model/UploadPartRequest.h>
#include <aws/s3/model/CreateMultipartUploadRequest.h>
#include <aws/s3/model/CompleteMultipartUploadRequest.h>
#include <aws/s3/model/AbortMultipartUploadRequest.h>
#include <aws/s3/model/DeleteObjectRequest.h>
#include <functional>
#include <streambuf>
#include <exception>
#include <cstring>
#include <limits>
#include <sstream>
#include <algorithm>
#include <memory>
#include <optional>
#include <thread>
#include <mutex>
#include <aws/s3/model/ListObjectVersionsRequest.h>
#include <aws/s3/model/DeleteBucketRequest.h>

namespace osv_atomic_streams {
/** Generate seekable constant input for actual SDK hashing/HTTP passes; invoke the original action at every positive EOF-reaching read before returning bytes. */
class ConstantBuffer : public std::streambuf {
    std::streamoff size,position=0;char value;std::function<void()> action;
    void at_end(){if(position==size){++eof_reads;if(action)try{action();}catch(...){error=std::current_exception();throw;}}}
protected:
    std::streamsize xsgetn(char* out,std::streamsize maximum) override {
        if(maximum<=0 || position>=size)return 0;
        auto count=std::min<std::streamoff>(maximum,size-position);std::memset(out,value,static_cast<size_t>(count));position+=count;at_end();return count;
    }
    int_type underflow() override {return position<size?traits_type::to_int_type(value):traits_type::eof();}
    int_type uflow() override {if(position>=size)return traits_type::eof();++position;at_end();return traits_type::to_int_type(value);}
    pos_type seekoff(off_type offset,std::ios_base::seekdir direction,std::ios_base::openmode mode) override {
        auto target=(direction==std::ios_base::beg?0:direction==std::ios_base::cur?position:size)+offset;
        if(!(mode&std::ios_base::in) || target<0 || target>size)return pos_type(off_type(-1));position=target;return pos_type(position);
    }
    pos_type seekpos(pos_type target,std::ios_base::openmode mode) override{return seekoff(off_type(target),std::ios_base::beg,mode);}
public:
    long long eof_reads=0;std::exception_ptr error;
    ConstantBuffer(long long length,char byte,std::function<void()> callback={}):size(length),value(byte),action(std::move(callback)){}
};

/** Verify actual HTTP response writes while transfer is ongoing; the first positive sink write synchronously calls the original writer before remaining response bytes resume. */
class VerifyingBuffer : public std::streambuf {
    long long expected;char value;std::function<void()> action;bool interrupted=false;
protected:
    std::streamsize xsputn(const char* data,std::streamsize size) override {
        try {
            for(std::streamsize i=0;i<size;++i)if(data[i]!=value)throw std::runtime_error("atomic download byte differs at "+std::to_string(count+i));
            count+=size;if(size>0)++writes;
            if(!interrupted && action && count>0){action();interrupted=true;++callbacks;}
            return size;
        }catch(...){error=std::current_exception();throw;}
    }
    int_type overflow(int_type input) override {if(traits_type::eq_int_type(input,traits_type::eof()))return traits_type::not_eof(input);char byte=traits_type::to_char_type(input);return xsputn(&byte,1)==1?input:traits_type::eof();}
public:
    long long count=0,writes=0,callbacks=0;std::exception_ptr error;
    VerifyingBuffer(long long size,char byte,std::function<void()> callback={}):expected(size),value(byte),action(std::move(callback)){}
    void finish(){if(error)std::rethrow_exception(error);if(count!=expected)throw std::runtime_error("atomic download size "+std::to_string(count)+" != "+std::to_string(expected));}
};
/** Preserve official error XML for the SDK parser while streaming only actual successful-response bytes into the verifying sink; actual response status comes from the SDK header callback. */
class ResponseBuffer : public std::stringbuf {
    VerifyingBuffer& sink;
protected:
    std::streamsize xsputn(const char* data,std::streamsize size) override {return status>=200 && status<300?sink.sputn(data,size):std::stringbuf::xsputn(data,size);}
    int_type overflow(int_type value) override {if(traits_type::eq_int_type(value,traits_type::eof()))return traits_type::not_eof(value);return status>=200 && status<300?sink.sputc(traits_type::to_char_type(value)):std::stringbuf::overflow(value);}
public:
    int status=0;
    explicit ResponseBuffer(VerifyingBuffer& destination):sink(destination){}
};
inline json capabilities(){
    json out=json::array({"atomic_stream_read","atomic_stream_write","atomic_stream_conditional_write","atomic_versions_overlap"});
    if(std::stoul(env("OSV_CONNECTIONS","16"))>=2){out.push_back("atomic_stream_dual_write");out.push_back("atomic_stream_multipart_resend");out.push_back("atomic_stream_dual_conditional_write");out.push_back("atomic_stream_bucket_gone");}
    return out;
}
inline std::string text(const json& q,const char* name){if(!q.contains(name)||!q.at(name).is_string()||q.at(name).get<std::string>().empty())throw std::runtime_error(std::string(name)+" must be nonempty string");return q.at(name).get<std::string>();}
inline void put(Aws::S3::S3Client& client,const std::string& bucket,const std::string& key,long long size,char byte){
    ConstantBuffer buffer(size,byte);auto body=Aws::MakeShared<Aws::IOStream>("osv-atomic",&buffer);
    Aws::S3::Model::PutObjectRequest request;request.SetBucket(bucket.c_str());request.SetKey(key.c_str());request.SetContentLength(size);request.SetBody(body);
    auto outcome=client.PutObject(request);if(buffer.error)std::rethrow_exception(buffer.error);checked(outcome);
}
inline void verify(Aws::S3::S3Client& client,const std::string& bucket,const std::string& key,VerifyingBuffer& buffer){
    Aws::S3::Model::GetObjectRequest request;request.SetBucket(bucket.c_str());request.SetKey(key.c_str());
    ResponseBuffer response(buffer);
    request.SetHeadersReceivedEventHandler([&response](const Aws::Http::HttpRequest*,Aws::Http::HttpResponse* value){response.status=static_cast<int>(value->GetResponseCode());});
    request.SetResponseStreamFactory([&response](){return Aws::New<Aws::IOStream>("osv-atomic-sink",&response);});
    auto outcome=client.GetObject(request);if(buffer.error)std::rethrow_exception(buffer.error);checked(outcome);buffer.finish();
}
inline long long independent_verify(const std::string& bucket,const std::string& key,long long size,char byte){
    S3Identities verifier;VerifyingBuffer body(size,byte);verify(*verifier.get("main"),bucket,key,body);return body.count;
}
inline json upload(Aws::S3::S3Client& original,const std::string& bucket,const std::string& key,long long size,const std::string& mode){
    bool dual=mode=="dual_write",conditional=mode=="conditional_write";
    put(original,bucket,key,dual?0:size,'A');
    if(!dual && !conditional)independent_verify(bucket,key,size,'A');
    long long callbacks=0;
    ConstantBuffer buffer(size,'B',[&](){++callbacks;if(dual)put(original,bucket,key,size,'A');else independent_verify(bucket,key,size,'A');});
    auto body=Aws::MakeShared<Aws::IOStream>("osv-atomic-upload",&buffer);
    Aws::S3::Model::PutObjectRequest request;request.SetBucket(bucket.c_str());request.SetKey(key.c_str());request.SetContentLength(size);request.SetBody(body);
    if(conditional)request.SetAdditionalCustomHeaderValue("If-Match","*");
    auto outcome=original.PutObject(request);if(buffer.error)std::rethrow_exception(buffer.error);checked(outcome);
    auto final_size=independent_verify(bucket,key,size,'B');
    return {{"mode",mode},{"eof_callbacks",callbacks},{"final_size",final_size}};
}
inline void callback_put(Aws::S3::S3Client& client,const std::string& bucket,const std::string& key,long long size,char byte,const std::optional<std::string>& condition,std::function<void()> action,long long& callbacks,std::exception_ptr& callback_error){
    ConstantBuffer buffer(size,byte,[&](){++callbacks;action();});auto body=Aws::MakeShared<Aws::IOStream>("osv-atomic-callback",&buffer);
    Aws::S3::Model::PutObjectRequest request;request.SetBucket(bucket.c_str());request.SetKey(key.c_str());request.SetContentLength(size);request.SetBody(body);
    if(condition)request.SetAdditionalCustomHeaderValue("If-Match",condition->c_str());
    try{auto outcome=client.PutObject(request);if(buffer.error){callback_error=buffer.error;std::rethrow_exception(buffer.error);}checked(outcome);}
    catch(...){if(buffer.error){callback_error=buffer.error;std::rethrow_exception(buffer.error);}throw;}
}
inline json dual_conditional(Aws::S3::S3Client& client,const std::string& bucket,const std::string& key,long long size){
    ConstantBuffer initial(size,'A');auto body=Aws::MakeShared<Aws::IOStream>("osv-atomic-initial",&initial);
    Aws::S3::Model::PutObjectRequest request;request.SetBucket(bucket.c_str());request.SetKey(key.c_str());request.SetContentLength(size);request.SetBody(body);
    json http;Aws::String match;
    {HttpObservationScope observation(&http);auto first=client.PutObject(request);checked(first);if(!http.contains("http_headers") || !http.at("http_headers").contains("etag"))throw std::runtime_error("actual initial A ETag is missing");match=first.GetResult().GetETag();}
    independent_verify(bucket,key,size,'A');match.erase(std::remove(match.begin(),match.end(),'"'),match.end());
    long long callbacks=0;std::exception_ptr callback_error;std::unique_ptr<SdkError> observed;
    try{callback_put(client,bucket,key,size,'C',std::optional<std::string>(match.c_str()),[&](){
        ConstantBuffer nested(size,'B');auto data=Aws::MakeShared<Aws::IOStream>("osv-atomic-nested-B",&nested);
        Aws::S3::Model::PutObjectRequest put;put.SetBucket(bucket.c_str());put.SetKey(key.c_str());put.SetContentLength(size);put.SetBody(data);put.SetAdditionalCustomHeaderValue("If-Match",match);
        checked(client.PutObject(put));
    },callbacks,callback_error);}
    catch(const SdkError& error){if(error.status!=412 || error.error_code!="PreconditionFailed")throw;observed=std::make_unique<SdkError>(error);}
    if(!observed)throw std::runtime_error("source expected actual 412/PreconditionFailed, outer C upload succeeded");
    auto final_size=independent_verify(bucket,key,size,'B');
    return {{"mode","dual_conditional_write"},{"eof_callbacks",callbacks},{"observed_error_status",observed->status},{"observed_error_code",observed->error_code},{"error_origin",callback_error?"nested_callback":"outer_put"},{"final_size",final_size}};
}
inline json bucket_gone(Aws::S3::S3Client& client,const std::string& bucket,const std::string& key,long long size){
    long long callbacks=0;std::exception_ptr callback_error;std::unique_ptr<SdkError> observed;
    try{callback_put(client,bucket,key,size,'A',std::nullopt,[&](){Aws::S3::Model::DeleteBucketRequest remove;remove.SetBucket(bucket.c_str());checked(client.DeleteBucket(remove));},callbacks,callback_error);}
    catch(const SdkError& error){if(error.status!=404 || error.error_code!="NoSuchBucket")throw;observed=std::make_unique<SdkError>(error);}
    if(!observed)throw std::runtime_error("source expected actual 404/NoSuchBucket, upload succeeded");
    return {{"mode","bucket_gone"},{"eof_callbacks",callbacks},{"observed_error_status",observed->status},{"observed_error_code",observed->error_code},{"error_origin",callback_error?"nested_callback":"outer_put"}};
}
inline Aws::String part(Aws::S3::S3Client& client,const std::string& bucket,const std::string& key,const Aws::String& upload,char byte,long long& eof,std::function<void()> action={}){
    ConstantBuffer buffer(8,byte,[&](){++eof;if(action)action();});auto body=Aws::MakeShared<Aws::IOStream>("osv-atomic-part",&buffer);
    Aws::S3::Model::UploadPartRequest request;request.SetBucket(bucket.c_str());request.SetKey(key.c_str());request.SetUploadId(upload);request.SetPartNumber(1);request.SetContentLength(8);request.SetBody(body);
    json http;HttpObservationScope observation(&http);auto outcome=client.UploadPart(request);if(buffer.error)std::rethrow_exception(buffer.error);checked(outcome);if(!http.contains("http_headers") || !http.at("http_headers").contains("etag"))throw std::runtime_error("actual UploadPart ETag is missing");return outcome.GetResult().GetETag();
}
inline Aws::String strip_quotes(const Aws::String& value){size_t start=0,end=value.size();while(start<end && value[start]=='"')++start;while(end>start && value[end-1]=='"')--end;return value.substr(start,end-start);}
inline Aws::S3::Model::CompletedPart completed_part(const Aws::String& etag){Aws::S3::Model::CompletedPart result;result.SetPartNumber(1);result.SetETag(strip_quotes(etag));return result;}
inline void complete(Aws::S3::S3Client& client,const std::string& bucket,const std::string& key,const Aws::String& upload,const Aws::Vector<Aws::S3::Model::CompletedPart>& parts){
    Aws::S3::Model::CompletedMultipartUpload value;value.SetParts(parts);
    Aws::S3::Model::CompleteMultipartUploadRequest request;request.SetBucket(bucket.c_str());request.SetKey(key.c_str());request.SetUploadId(upload);request.SetMultipartUpload(value);checked(client.CompleteMultipartUpload(request));
}
inline Aws::String create(Aws::S3::S3Client& client,const std::string& bucket,const std::string& key,Aws::Vector<Aws::String>& pending){
    Aws::S3::Model::CreateMultipartUploadRequest request;request.SetBucket(bucket.c_str());request.SetKey(key.c_str());auto outcome=client.CreateMultipartUpload(request);checked(outcome);
    auto upload=outcome.GetResult().GetUploadId();if(upload.empty())throw std::runtime_error("actual CreateMultipartUpload UploadId is missing");pending.push_back(upload);return upload;
}
inline json multipart(Aws::S3::S3Client& client,const std::string& bucket,const std::string& key){
    Aws::Vector<Aws::String> pending;std::exception_ptr primary,cleanup;json result;
    try {
        auto dry=create(client,bucket,key,pending);long long dry_eof=0;
        auto dry_tag=part(client,bucket,key,dry,'C',dry_eof);complete(client,bucket,key,dry,{completed_part(dry_tag)});pending.erase(std::find(pending.begin(),pending.end(),dry));
        Aws::S3::Model::DeleteObjectRequest remove;remove.SetBucket(bucket.c_str());remove.SetKey(key.c_str());checked(client.DeleteObject(remove));
        auto actual=create(client,bucket,key,pending);long long outer_eof=0,nested_eof=0,triggered=0;Aws::Vector<Aws::S3::Model::CompletedPart> parts;
        auto outer=part(client,bucket,key,actual,'A',outer_eof,[&](){if(outer_eof==dry_eof){auto nested=part(client,bucket,key,actual,'B',nested_eof);parts.push_back(completed_part(nested));++triggered;}});
        parts.push_back(completed_part(outer));complete(client,bucket,key,actual,parts);pending.erase(std::find(pending.begin(),pending.end(),actual));
        auto final_size=independent_verify(bucket,key,8,'A');
        result={{"mode","multipart_resend"},{"dry_eof_reads",dry_eof},{"outer_eof_reads",outer_eof},{"nested_eof_reads",nested_eof},{"nested_actions",triggered},{"completed_entries",parts.size()},{"final_size",final_size}};
    }catch(...){primary=std::current_exception();}
    for(const auto& upload:pending)try{
        Aws::S3::Model::AbortMultipartUploadRequest request;request.SetBucket(bucket.c_str());request.SetKey(key.c_str());request.SetUploadId(upload);auto outcome=client.AbortMultipartUpload(request);
        if(!outcome.IsSuccess() && static_cast<int>(outcome.GetError().GetResponseCode())!=404)checked(outcome);
    }catch(...){if(!cleanup)cleanup=std::current_exception();}
    if(primary)std::rethrow_exception(primary);if(cleanup)std::rethrow_exception(cleanup);return result;
}
inline void start_thread(std::vector<std::thread>& threads,std::function<void()> action,json& failures,std::mutex& mutex){
    threads.emplace_back([action,&failures,&mutex](){try{action();}
        catch(const SdkError& error){std::lock_guard<std::mutex> lock(mutex);failures.push_back({{"error",error.what()},{"status_code",error.status},{"error_code",error.error_code}});}
        catch(const std::exception& error){std::lock_guard<std::mutex> lock(mutex);failures.push_back({{"error",error.what()}});}
        catch(...){std::lock_guard<std::mutex> lock(mutex);failures.push_back({{"error","non-standard SDK thread exception"}});}
    });
}
inline void join_threads(std::vector<std::thread>& threads){for(auto& thread:threads)if(thread.joinable())thread.join();}
inline size_t start_deletes(Aws::S3::S3Client& client,const std::string& bucket,std::vector<std::thread>& threads,json& failures,std::mutex& mutex){
    Aws::S3::Model::ListObjectVersionsRequest request;request.SetBucket(bucket.c_str());auto outcome=client.ListObjectVersions(request);checked(outcome);
    for(const auto& version:outcome.GetResult().GetVersions()){
        if(!version.KeyHasBeenSet() || !version.VersionIdHasBeenSet())throw std::runtime_error("actual listed version is missing Key/VersionId; refusing unversioned delete fallback");
        start_thread(threads,[&client,bucket,version](){Aws::S3::Model::DeleteObjectRequest remove;remove.SetBucket(bucket.c_str());remove.SetKey(version.GetKey());remove.SetVersionId(version.GetVersionId());checked(client.DeleteObject(remove));},failures,mutex);
    }
    return outcome.GetResult().GetVersions().size();
}
inline json versioned_overlap(Aws::S3::S3Client& client,const std::string& bucket,const std::string& key){
    std::vector<std::thread> threads;json failures=json::array(),groups=json::array(),result;std::mutex mutex;std::exception_ptr primary;
    try{
        for(int round=0;round<3;++round){
            for(int i=0;i<3;++i)start_thread(threads,[&client,bucket,key,i](){
                auto body=Aws::MakeShared<Aws::StringStream>("osv-original-version-body");*body<<"data "<<i;
                Aws::S3::Model::PutObjectRequest request;request.SetBucket(bucket.c_str());request.SetKey(key.c_str());request.SetBody(body);checked(client.PutObject(request));
            },failures,mutex);
            groups.push_back(start_deletes(client,bucket,threads,failures,mutex));
        }
        join_threads(threads);groups.push_back(start_deletes(client,bucket,threads,failures,mutex));join_threads(threads);
        Aws::S3::Model::ListObjectVersionsRequest request;request.SetBucket(bucket.c_str());json http;
        {HttpObservationScope observation(&http,true);auto final_list=client.ListObjectVersions(request);checked(final_list);}
        if(!http.contains("xml_root") || http.at("xml_root").is_null() || !http.contains("fields"))throw std::runtime_error("actual final ListObjectVersions XML observation unavailable");
        auto fields=http.at("fields").get<std::set<std::string>>();bool present=fields.count("Version")!=0;
        if(present)throw std::runtime_error("source expected Versions absent after overlapping create/remove and final cleanup");
        result={{"mode","versioned_overlap"},{"source_put_threads",9},{"actual_delete_group_sizes",groups},{"thread_errors",failures},{"versions_present",present}};
    }catch(...){primary=std::current_exception();}
    join_threads(threads);if(primary)std::rethrow_exception(primary);return result;
}
inline json execute(Aws::S3::S3Client& original,const json& q){
    auto mode=text(q,"mode");
    if(mode!="read" && mode!="write" && mode!="dual_write" && mode!="conditional_write" && mode!="multipart_resend" && mode!="dual_conditional_write" && mode!="bucket_gone" && mode!="versioned_overlap")throw std::runtime_error("atomic stream mode not implemented");
    if((mode=="dual_write" || mode=="multipart_resend" || mode=="dual_conditional_write" || mode=="bucket_gone") && std::stoul(env("OSV_CONNECTIONS","16"))<2)throw std::runtime_error("same-client nested upload requires OSV_CONNECTIONS >= 2");
    if(!q.contains("size")||!q.at("size").is_number_integer()||(q.at("size").is_number_unsigned() && q.at("size").get<uint64_t>()>static_cast<uint64_t>(std::numeric_limits<long long>::max()))||q.at("size").get<long long>()<1)throw std::runtime_error("atomic size must be positive integer");
    if(q.contains("identity") && q.at("identity")!="main")throw std::runtime_error("atomic source requires main identity");
    auto bucket=text(q,"bucket"),key=text(q,"key");auto size=q.at("size").get<long long>();
    if(mode=="versioned_overlap")return versioned_overlap(original,bucket,key);
    if(mode=="dual_conditional_write")return dual_conditional(original,bucket,key,size);
    if(mode=="bucket_gone")return bucket_gone(original,bucket,key,size);
    if(mode=="multipart_resend"){if(size!=8)throw std::runtime_error("original multipart resend size must be 8 bytes");return multipart(original,bucket,key);}
    if(mode!="read")return upload(original,bucket,key,size,mode);
    put(original,bucket,key,size,'A');
    VerifyingBuffer ongoing(size,'A',[&](){put(original,bucket,key,size,'B');});
    {S3Identities readers;verify(*readers.get("main"),bucket,key,ongoing);}
    VerifyingBuffer final_body(size,'B');
    {S3Identities verifier;verify(*verifier.get("main"),bucket,key,final_body);}
    return {{"mode","read"},{"read_size",ongoing.count},{"read_writes",ongoing.writes},{"callbacks",ongoing.callbacks},{"final_size",final_body.count}};
}
}
