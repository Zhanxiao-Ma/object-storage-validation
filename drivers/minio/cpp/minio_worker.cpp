#include <miniocpp/client.h>
#include <miniocpp/config.h>
#include <nlohmann/json.hpp>
#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include <vector>
using json=nlohmann::json;
static std::string env(const std::string& name,const std::string& fallback="") {const char* value=std::getenv(("OSV_"+name).c_str());return value?value:fallback;}
static std::string required(const std::string& name) {auto value=env(name);if(value.empty())throw std::runtime_error("missing OSV_"+name);return value;}

/** Runs real MinIO S3 requests with explicit region and path style for comparable workloads.
 * Single-request APIs preserve PUT/part boundaries; streaming GET writes actual response bytes
 * before returning to the persistent JSONL scheduler. No HTTP request implementation is substituted.
 */
class MinioWorker {
    minio::s3::BaseUrl url;
    minio::creds::StaticProvider credentials;
    minio::s3::Client client;
    std::string bucket=required("BUCKET"),region=env("REGION","us-east-1");
    static minio::s3::BaseUrl endpoint() {
        auto value=required("ENDPOINT");bool tls=value.rfind("https://",0)==0;
        if(!tls && value.rfind("http://",0)!=0)throw std::runtime_error("invalid endpoint scheme");
        auto host=value.substr(tls?8:7);if(!host.empty() && host.back()=='/')host.pop_back();
        minio::s3::BaseUrl result(host,tls,env("REGION","us-east-1"));
        if(!result)throw std::runtime_error(result.Error().String());
        result.virtual_style=env("PATH_STYLE","true")!="true";return result;
    }
    template<class Args> Args args(const json& q) {
        Args a;a.bucket=bucket;a.region=region;a.object=q.value("key","");return a;
    }
    template<class T> T checked(minio::Result<T> result) {
        if(!result)throw std::runtime_error(result.error().String());
        if(result->status_code!=0 && (result->status_code<200 || result->status_code>=300))throw std::runtime_error("HTTP "+std::to_string(result->status_code)+" "+result->code+": "+result->message);
        return std::move(*result);
    }
    static std::string read(const std::string& path) {
        std::ifstream input(path,std::ios::binary|std::ios::ate);if(!input)throw std::runtime_error("cannot open payload");
        auto size=input.tellg();if(size<0)throw std::runtime_error("payload size failed");
        std::string data(static_cast<size_t>(size),char{});input.seekg(0);
        if(size>0 && !input.read(data.data(),size))throw std::runtime_error("payload read failed");
        return data;
    }
public:
    MinioWorker():url(endpoint()),credentials(required("ACCESS_KEY"),required("SECRET_KEY"),env("SESSION_TOKEN")),client(url,&credentials) {
        if(env("RETRIES","0")!="0")throw std::runtime_error("MinIO worker requires OSV_RETRIES=0");
    }
    json execute(const json& q) {
        auto op=q.at("op").get<std::string>();json out=json::object();
        if(op=="info")return {{"language","cpp"},{"api","s3"},{"backend","minio"},{"sdk_version",MINIO_CPP_VERSION},{"protocol_version",1},
            {"capabilities",{"put","get","head","list","delete","mp_create","mp_part","mp_complete","mp_abort"}},
            {"endpoint",env("ENDPOINT")},{"region",region},{"bucket",bucket},{"retries","0"},{"connections_per_worker","SDK default; one synchronous request per worker"},
            {"measurement","SDK operation and payload file IO; full response consumed; excludes JSONL"}};
        if(op=="put") {
            auto data=read(q.at("body_path"));auto a=args<minio::s3::PutObjectApiArgs>(q);a.data=data;a.buf=nullptr;a.size=data.size();a.object_size=data.size();a.content_type="application/octet-stream";
            out["etag"]=checked(client.BaseClient::PutObject(a)).etag;
        } else if(op=="get") {
            std::ofstream output(q.at("output_path").get<std::string>(),std::ios::binary);if(!output)throw std::runtime_error("cannot open output");
            auto a=args<minio::s3::GetObjectArgs>(q);if(q.contains("offset")){a.offset=q.at("offset").get<size_t>();a.length=q.at("length").get<size_t>();}
            a.datafunc=[&](minio::http::DataFunctionArgs chunk){output.write(chunk.datachunk.data(),chunk.datachunk.size());return static_cast<bool>(output);};
            checked(client.GetObject(a));output.close();if(!output)throw std::runtime_error("output write failed");
        } else if(op=="head")out["size"]=checked(client.StatObject(args<minio::s3::StatObjectArgs>(q))).size;
        else if(op=="delete")checked(client.RemoveObject(args<minio::s3::RemoveObjectArgs>(q)));
        else if(op=="list") {
            minio::s3::ListObjectsV2Args a;a.bucket=bucket;a.region=region;a.prefix=q.value("prefix","");a.max_keys=1000;
            std::vector<std::string> keys;std::string previous;
            for(;;){auto response=checked(client.ListObjectsV2(a));for(const auto& item:response.contents)keys.emplace_back(item.name);
                if(!response.is_truncated)break;
                std::string token(response.next_continuation_token);
                if(token.empty() || token==previous)throw std::runtime_error("stalled listing token");
                a.continuation_token=token;previous=token;}

            std::sort(keys.begin(),keys.end());out["keys"]=keys;
        } else if(op=="mp_create")out["upload_id"]=checked(client.CreateMultipartUpload(args<minio::s3::CreateMultipartUploadArgs>(q))).upload_id;
        else if(op=="mp_part") {
            auto data=read(q.at("body_path"));auto a=args<minio::s3::UploadPartArgs>(q);a.upload_id=q.at("upload_id");a.part_number=q.at("part_number");a.data=data;a.buf=nullptr;a.part_size=data.size();out["etag"]=checked(client.UploadPart(a)).etag;
        } else if(op=="mp_complete") {
            auto a=args<minio::s3::CompleteMultipartUploadArgs>(q);a.upload_id=q.at("upload_id");for(const auto& part:q.at("parts"))a.parts.emplace_back(part.at("part_number").get<unsigned int>(),part.at("etag").get<std::string>());out["etag"]=checked(client.CompleteMultipartUpload(a)).etag;
        } else if(op=="mp_abort") {auto a=args<minio::s3::AbortMultipartUploadArgs>(q);a.upload_id=q.at("upload_id");checked(client.AbortMultipartUpload(a));}
        else throw std::runtime_error("unsupported operation: "+op);
        return out;
    }
};
int main() {
    try {
        MinioWorker worker;std::string line;
        while(std::getline(std::cin,line)) {
            json result;auto start=std::chrono::steady_clock::now();
            try {auto request=json::parse(line);start=std::chrono::steady_clock::now();result=worker.execute(request);result["ok"]=true;}
            catch(const std::exception& e){result={{"ok",false},{"error_kind","sdk_error"},{"error",e.what()}};}
            result["elapsed_ns"]=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count();std::cout<<result.dump()<<std::endl;
        }
    }catch(const std::exception& e){std::cerr<<e.what()<<std::endl;return 1;}
}
