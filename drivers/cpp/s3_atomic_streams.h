#pragma once
#include <aws/s3/S3Client.h>
#include <aws/s3/model/GetObjectRequest.h>
#include <aws/s3/model/PutObjectRequest.h>
#include <functional>
#include <streambuf>
#include <exception>
#include <cstring>
#include <limits>

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
inline json capabilities(){return json::array({"atomic_stream_read"});}
inline std::string text(const json& q,const char* name){if(!q.contains(name)||!q.at(name).is_string()||q.at(name).get<std::string>().empty())throw std::runtime_error(std::string(name)+" must be nonempty string");return q.at(name).get<std::string>();}
inline void put(Aws::S3::S3Client& client,const std::string& bucket,const std::string& key,long long size,char byte){
    ConstantBuffer buffer(size,byte);auto body=Aws::MakeShared<Aws::IOStream>("osv-atomic",&buffer);
    Aws::S3::Model::PutObjectRequest request;request.SetBucket(bucket.c_str());request.SetKey(key.c_str());request.SetContentLength(size);request.SetBody(body);
    auto outcome=client.PutObject(request);if(buffer.error)std::rethrow_exception(buffer.error);checked(outcome);
}
inline void verify(Aws::S3::S3Client& client,const std::string& bucket,const std::string& key,VerifyingBuffer& buffer){
    Aws::S3::Model::GetObjectRequest request;request.SetBucket(bucket.c_str());request.SetKey(key.c_str());
    request.SetResponseStreamFactory([&buffer](){return Aws::New<Aws::IOStream>("osv-atomic-sink",&buffer);});
    auto outcome=client.GetObject(request);if(buffer.error)std::rethrow_exception(buffer.error);checked(outcome);buffer.finish();
}
inline json execute(Aws::S3::S3Client& original,const json& q){
    if(text(q,"mode")!="read")throw std::runtime_error("atomic stream mode not implemented");
    if(!q.contains("size")||!q.at("size").is_number_integer()||(q.at("size").is_number_unsigned() && q.at("size").get<uint64_t>()>static_cast<uint64_t>(std::numeric_limits<long long>::max()))||q.at("size").get<long long>()<1)throw std::runtime_error("atomic size must be positive integer");
    if(q.contains("identity") && q.at("identity")!="main")throw std::runtime_error("atomic source requires main identity");
    auto bucket=text(q,"bucket"),key=text(q,"key");auto size=q.at("size").get<long long>();
    put(original,bucket,key,size,'A');
    VerifyingBuffer ongoing(size,'A',[&](){put(original,bucket,key,size,'B');});
    {S3Identities readers;verify(*readers.get("main"),bucket,key,ongoing);}
    VerifyingBuffer final_body(size,'B');
    {S3Identities verifier;verify(*verifier.get("main"),bucket,key,final_body);}
    return {{"mode","read"},{"read_size",ongoing.count},{"read_writes",ongoing.writes},{"callbacks",ongoing.callbacks},{"final_size",final_body.count}};
}
}
