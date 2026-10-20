#pragma once
#include <future>
#include <functional>
#include <map>
#include <chrono>

/**
 * Executes interleaved source-test operations on the existing official S3 client.
 * Bounded futures retain each genuine SDK response, exception, response body and
 * duration; nonblocking polling lets other source threads submit their next call.
 * Futures are joined before the owning S3 client is destroyed.
 */
class S3AsyncJobs {
    std::function<json(const json&)> execute_request;
    std::map<std::string,std::future<json>> jobs;
    uint64_t next=0;
public:
    explicit S3AsyncJobs(std::function<json(const json&)> execute):execute_request(std::move(execute)){}
    static bool handles(const std::string& op){return op=="async_start" || op=="async_poll";}
    json execute(const json& q){
        if(q.at("op")=="async_poll"){
            if(!q.contains("job_id") || !q.at("job_id").is_string())throw std::runtime_error("job_id must be textual");
            auto id=q.at("job_id").get<std::string>();auto found=jobs.find(id);
            if(found==jobs.end())throw std::runtime_error("unknown or consumed async job");
            if(found->second.wait_for(std::chrono::seconds(0))!=std::future_status::ready)return {{"done",false}};
            auto result=found->second.get();jobs.erase(found);return {{"done",true},{"result",result}};
        }
        if(jobs.size()>=512)throw std::runtime_error("at most 512 outstanding async jobs");
        if(!q.contains("request") || !q.at("request").is_object() || !q.at("request").contains("op") || !q.at("request").at("op").is_string())throw std::runtime_error("async request requires textual op");
        auto request=q.at("request");auto op=request.at("op").get<std::string>();
        if(op.empty() || handles(op) || op=="info" || op=="concurrent_requests" || op=="select" || op=="presign" || op=="atomic_stream" || op.rfind("api.sns.",0)==0 || op.rfind("api.s3control.",0)==0 || op.rfind("api.iam.",0)==0 || op.rfind("api.sts.",0)==0)throw std::runtime_error("unsupported async operation");
        for(const auto* name:{"identity","wire_headers","wire_query","checksum_when_required"})if(request.contains(name))throw std::runtime_error(std::string("async requires the existing default S3 client: ")+name);
        if(request.contains("capture_response_body") && !request.at("capture_response_body").is_boolean())throw std::runtime_error("capture_response_body must be boolean");
        auto id=std::to_string(++next);
        jobs.emplace(id,std::async(std::launch::async,[this,request]{
            auto start=std::chrono::steady_clock::now();json result;
            osv_response_body::reset();
            try {osv_response_body::begin(request);result=execute_request(request);result["ok"]=true;}
            catch(const SdkError& error){result={{"ok",false},{"error",error.what()},{"error_kind",error.status==404?"not_found":"sdk_error"},{"status_code",error.status},{"error_code",error.error_code},{"error_message",error.what()},{"http_headers",error.headers}};}
            catch(const std::exception& error){result={{"ok",false},{"error",error.what()},{"error_kind","worker_error"}};}
            osv_response_body::attach(result);result["elapsed_ns"]=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count();return result;
        }));
        return {{"job_id",id}};
    }
};
