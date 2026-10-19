// The upstream C header has one member with the same name as its typedef.
// Scope GCC's compatibility diagnostic to that header, keeping our code strict.
#if defined(__GNUC__) && !defined(__clang__) && __GNUC__ >= 13
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wchanges-meaning"
#endif
#include <eSDKOBS.h>
#if defined(__GNUC__) && !defined(__clang__) && __GNUC__ >= 13
#pragma GCC diagnostic pop
#endif
#include <nlohmann/json.hpp>
#include <array>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <regex>
#include <stdexcept>
#include <string>
#include <vector>

using json = nlohmann::json;
static std::string env(const char* name, const std::string& fallback="") {
    const auto value=std::getenv(name); return value ? value : fallback;
}
static std::string required(const char* name) {
    auto value=env(name); if(value.empty()) throw std::runtime_error(std::string("missing ")+name); return value;
}

/**
 * Preserve the native C SDK status without hiding authorization or transport errors.
 * Only the SDK's object-missing and HTTP-not-found statuses map to not_found.
 */
class ObsError : public std::runtime_error {
public:
    bool missing;
    ObsError(obs_status status, const std::string& detail)
        :std::runtime_error(std::string(obs_get_status_name(status))+": "+detail),
         missing(status==OBS_STATUS_NoSuchKey || status==OBS_STATUS_HttpErrorNotFound) {}
};

/**
 * Keep callback data alive until the synchronous C API returns.
 * Copy SDK-owned response fields and trap C++ exceptions before they can cross the C ABI.
 */
struct Response {
    obs_status status=OBS_STATUS_InternalError;
    bool completed=false;
    bool callback_failed=false;
    uint64_t size=0;
    std::string etag, detail, next_marker;
    bool truncated=false;
    std::vector<std::string> keys;
    std::ifstream input;
    std::ofstream output;
};

static obs_status properties(const obs_response_properties* fields, void* context) noexcept {
    auto& state=*static_cast<Response*>(context);
    try {
        state.size=fields->content_length;
        state.etag=fields->etag ? fields->etag : "";
        return OBS_STATUS_OK;
    } catch(...) { state.callback_failed=true; return OBS_STATUS_AbortedByCallback; }
}
static void complete(obs_status status, const obs_error_details* error, void* context) noexcept {
    auto& state=*static_cast<Response*>(context);
    state.status=status; state.completed=true;
    try { if(error && error->message) state.detail=error->message; }
    catch(...) { state.callback_failed=true; }
}
static int read_body(int capacity, char* buffer, void* context) noexcept {
    auto& state=*static_cast<Response*>(context);
    try {
        state.input.read(buffer,capacity);
        if(state.input.bad() || (state.input.fail() && !state.input.eof())) {
            state.callback_failed=true; return -1;
        }
        return static_cast<int>(state.input.gcount());
    } catch(...) { state.callback_failed=true; return -1; }
}
static obs_status write_body(int size, const char* buffer, void* context) noexcept {
    auto& state=*static_cast<Response*>(context);
    try {
        state.output.write(buffer,size);
        if(!state.output) { state.callback_failed=true; return OBS_STATUS_AbortedByCallback; }
        return OBS_STATUS_OK;
    } catch(...) { state.callback_failed=true; return OBS_STATUS_AbortedByCallback; }
}
static obs_status list_page(int truncated, const char* marker, int count,
    const obs_list_objects_content* contents, int, const char**, void* context) noexcept {
    auto& state=*static_cast<Response*>(context);
    try {
        state.truncated=truncated!=0;
        state.next_marker=marker ? marker : "";
        for(int i=0;i<count;++i) {
            if(!contents[i].key) { state.callback_failed=true; return OBS_STATUS_AbortedByCallback; }
            state.keys.emplace_back(contents[i].key);
        }
        return OBS_STATUS_OK;
    } catch(...) { state.callback_failed=true; return OBS_STATUS_AbortedByCallback; }
}
static obs_status multipart_result(const char*, const char*, const char*, const char* etag, void* context) noexcept {
    auto& state=*static_cast<Response*>(context);
    try { state.etag=etag ? etag : ""; return OBS_STATUS_OK; }
    catch(...) { state.callback_failed=true; return OBS_STATUS_AbortedByCallback; }
}
static void quiet_log(OBS_LOGLEVEL, char*, size_t) noexcept {}

/**
 * Initialize the real C SDK once per worker and release it on every exit path.
 * RAII prevents partially initialized adapters from leaking global SDK resources.
 */
class ObsRuntime {
public:
    ObsRuntime() {
        setUserCustomLog(quiet_log);
        auto status=obs_initialize(OBS_INIT_ALL);
        if(status!=OBS_STATUS_OK) throw ObsError(status,"SDK initialization failed");
    }
    ~ObsRuntime() { obs_deinitialize(); }
    ObsRuntime(const ObsRuntime&)=delete;
    ObsRuntime& operator=(const ObsRuntime&)=delete;
};

/**
 * Expose OBS native operations to C++ workloads through Huawei's C SDK.
 * Force OBS authentication, retain native errors, and stream actual payloads through C callbacks.
 */
class ObsWorker {
    std::string bucket=required("OSV_BUCKET"), endpoint=required("OSV_ENDPOINT");
    std::string access_key=required("OSV_ACCESS_KEY"), secret_key=required("OSV_SECRET_KEY");
    std::string token=env("OSV_SESSION_TOKEN"), certificate=env("OSV_CA_BUNDLE"), authority;
    obs_options options{};
    static void check(const Response& state) {
        if(state.callback_failed) throw std::runtime_error("OBS callback failed while copying response or streaming payload");
        if(!state.completed) throw std::runtime_error("OBS SDK did not invoke its completion callback");
        if(state.status!=OBS_STATUS_OK) throw ObsError(state.status,state.detail);
    }
    static uint64_t open_body(Response& state, const json& q) {
        state.input.open(q.at("body_path").get<std::string>(),std::ios::binary);
        if(!state.input) throw std::runtime_error("cannot open body_path");
        state.input.seekg(0,std::ios::end); auto length=state.input.tellg(); state.input.seekg(0);
        if(length<0 || !state.input) throw std::runtime_error("cannot determine payload length");
        return static_cast<uint64_t>(length);
    }
public:
    ObsWorker() {
        if(env("OSV_RETRIES","sdk-default")!="sdk-default")
            throw std::runtime_error("OBS C SDK retry policy is not configurable; set OSV_RETRIES=sdk-default");
        if(env("OSV_PATH_STYLE","false")!="false")
            throw std::runtime_error("OBS native mode requires virtual-host style; Path Style selects S3 in the C SDK");
        std::smatch match;
        const std::regex url(R"(^(https?)://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(:[0-9]{1,5})?/?$)");
        if(!std::regex_match(endpoint,match,url))
            throw std::runtime_error("OBS endpoint must be an http(s) service-domain URL without path or credentials");
        if(match[3].matched) {
            auto port=std::stoul(match[3].str().substr(1));
            if(port==0 || port>65535) throw std::runtime_error("invalid endpoint port");
        }
        authority=match[2].str()+match[3].str();
        init_obs_options(&options);
        options.bucket_options.bucket_name=bucket.data(); options.bucket_options.host_name=authority.data();
        options.bucket_options.access_key=access_key.data(); options.bucket_options.secret_access_key=secret_key.data();
        options.bucket_options.token=token.empty() ? nullptr : token.data();
        options.bucket_options.protocol=match[1].str()=="https" ? OBS_PROTOCOL_HTTPS : OBS_PROTOCOL_HTTP;
        options.bucket_options.uri_style=OBS_URI_STYLE_VIRTUALHOST;
        options.bucket_options.useCname=env("OSV_CNAME","false")=="true";
        options.request_options.auth_switch=OBS_OBS_TYPE;
        options.request_options.connect_time=10000; options.request_options.max_connected_time=30;
        options.request_options.keep_alive=true; options.request_options.forbid_reuse_tcp=false;
        options.request_options.curl_max_connects=std::stol(env("OSV_CONNECTIONS","16"));
        options.request_options.ssl_verify_host=OBS_SSL_VERIFYHOST_OPEN;
        options.request_options.ssl_verify_peer=OBS_SSL_VERIFYPEER_OPEN;
        if(!certificate.empty()) options.request_options.server_cert_path=certificate.data();
    }
    json execute(const json& q) {
        std::string op=q.at("op"),key=q.value("key","");
        if(op=="info") return {{"backend","obs"},{"language","cpp"},{"sdk_language","c"},{"api","native"},
            {"sdk","huaweicloud-sdk-c-obs"},{"sdk_version",OSV_OBS_SDK_VERSION},{"sdk_source_revision",OSV_OBS_SOURCE_REVISION},
            {"protocol_version",1},{"capabilities",{"put","get","head","list","delete","mp_create","mp_part","mp_complete","mp_abort"}},
            {"endpoint",endpoint},{"bucket",bucket},{"region",env("OSV_REGION")},{"auth_mode","OBS_OBS_TYPE"},
            {"path_style",false},{"cname",options.bucket_options.useCname},{"retries","sdk-default; not configurable"},
            {"sdk_retry_loop_budget",OSV_OBS_ATTEMPTS},{"connections_per_worker",env("OSV_CONNECTIONS","16")},
            {"connection_limit_semantics","libcurl cached connections; not a total active connection cap"},
            {"tls_verify_peer",true},{"tls_verify_hostname",true},
            {"measurement","operation including local payload IO; excludes JSONL serialization"}};
        Response state;
        obs_response_handler response_handler{properties,complete};
        obs_object_info object{}; object.key=key.data();
        if(op=="put") {
            auto length=open_body(state,q);
            obs_put_properties props{}; init_put_properties(&props);
            obs_put_object_handler handler{response_handler,read_body,nullptr};
            put_object(&options,key.data(),length,&props,nullptr,&handler,&state);
        } else if(op=="get") {
            state.output.open(q.at("output_path").get<std::string>(),std::ios::binary|std::ios::trunc);
            if(!state.output) throw std::runtime_error("cannot open output_path");
            obs_get_conditions conditions{}; init_get_properties(&conditions);
            if(q.contains("offset")) {
                conditions.start_byte=q.at("offset").get<uint64_t>(); conditions.byte_count=q.at("length").get<uint64_t>();
                if(conditions.byte_count==0) throw std::runtime_error("Range length must be positive");
            }
            obs_get_object_handler handler{response_handler,write_body,nullptr};
            get_object(&options,&object,&conditions,nullptr,&handler,&state);
            state.output.close(); if(!state.output) state.callback_failed=true;
        } else if(op=="head") {
            get_object_metadata(&options,&object,nullptr,&response_handler,&state); check(state); return {{"size",state.size}};
        } else if(op=="delete") {
            delete_object(&options,&object,&response_handler,&state);
        } else if(op=="list") {
            json keys=json::array(); std::string marker,prefix=q.at("prefix");
            for(;;) {
                Response page;
                obs_list_objects_handler handler{response_handler,list_page};
                list_bucket_objects(&options,prefix.c_str(),marker.empty()?nullptr:marker.c_str(),nullptr,1000,&handler,&page);
                check(page); for(const auto& name:page.keys) keys.push_back(name);
                if(!page.truncated) break;
                if(page.next_marker.empty() || page.next_marker==marker) throw std::runtime_error("invalid pagination marker");
                marker=page.next_marker;
            }
            return {{"keys",keys}};
        } else if(op=="mp_create") {
            std::array<char,8192> upload_id{};
            obs_put_properties props{}; init_put_properties(&props);
            initiate_multi_part_upload(&options,key.data(),upload_id.size(),upload_id.data(),&props,nullptr,&response_handler,&state);
            check(state); if(!upload_id[0]) throw std::runtime_error("missing upload ID");
            return {{"upload_id",upload_id.data()}};
        } else if(op=="mp_part") {
            auto length=open_body(state,q); std::string upload_id=q.at("upload_id");
            obs_upload_part_info part{}; part.upload_id=upload_id.data(); part.part_number=q.at("part_number").get<unsigned>();
            obs_put_properties props{}; init_put_properties(&props);
            obs_upload_handler handler{response_handler,read_body,nullptr};
            upload_part(&options,key.data(),&part,length,&props,nullptr,&handler,&state);
            check(state); if(state.etag.empty()) throw std::runtime_error("missing part ETag");
            return {{"etag",state.etag}};
        } else if(op=="mp_complete") {
            std::string upload_id=q.at("upload_id"); std::vector<std::string> etags;
            for(const auto& part:q.at("parts")) etags.push_back(part.at("etag").get<std::string>());
            std::vector<obs_complete_upload_Info> parts; size_t index=0;
            for(const auto& part:q.at("parts")) parts.push_back({part.at("part_number").get<unsigned>(),etags[index++].data()});
            obs_put_properties props{}; init_put_properties(&props);
            obs_complete_multi_part_upload_handler handler{response_handler,multipart_result};
            complete_multi_part_upload(&options,key.data(),upload_id.c_str(),parts.size(),parts.data(),&props,&handler,&state);
        } else if(op=="mp_abort") {
            std::string upload_id=q.at("upload_id");
            abort_multi_part_upload(&options,key.data(),upload_id.c_str(),&response_handler,&state);
        } else throw std::runtime_error("unsupported operation: "+op);
        check(state); return json::object();
    }
};

int main() {
    try {
        ObsRuntime runtime; ObsWorker worker;
        for(std::string line;std::getline(std::cin,line);) {
            auto start=std::chrono::steady_clock::now(); json result;
            try { result=worker.execute(json::parse(line)); result["ok"]=true; }
            catch(const ObsError& error) { result={{"ok",false},{"error",error.what()},{"error_kind",error.missing?"not_found":"sdk_error"}}; }
            catch(const std::exception& error) { result={{"ok",false},{"error",error.what()},{"error_kind","worker_error"}}; }
            result["elapsed_ns"]=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count();
            std::cout<<result.dump()<<std::endl;
        }
    } catch(const std::exception& error) { std::cerr<<error.what()<<std::endl; return 2; }
}
