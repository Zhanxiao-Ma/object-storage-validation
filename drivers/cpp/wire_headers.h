#pragma once
#include <aws/core/auth/signer/AWSAuthSignerBase.h>
#include <aws/core/auth/signer-provider/AWSAuthSignerProviderBase.h>
#include <mutex>
#include <map>

inline thread_local const json* osv_wire_headers=nullptr;

/** Restore a validated per-call before-signing patch on all exit paths, leaving normal SDK operations untouched. */
class WireHeaderScope {
    const json* previous;
public:
    explicit WireHeaderScope(const json& patch):previous(osv_wire_headers) {
        if(!patch.is_object())throw std::runtime_error("wire_headers must be an object");
        std::set<std::string> names;
        for(auto it=patch.begin();it!=patch.end();++it) {
            auto name=it.key();
            if(name.empty())throw std::runtime_error("invalid header name");
            for(auto& character:name) {
                auto byte=static_cast<unsigned char>(character);
                if(!((byte>='a'&&byte<='z')||(byte>='A'&&byte<='Z')||(byte>='0'&&byte<='9')||std::string("!#$%&'*+-.^_`|~").find(character)!=std::string::npos))throw std::runtime_error("invalid header name");
                if(byte>='A'&&byte<='Z')character=static_cast<char>(byte-'A'+'a');
            }
            if(!names.insert(name).second)throw std::runtime_error("duplicate header name");
            if(!it.value().is_null()) {
                if(!it.value().is_string())throw std::runtime_error("wire header values must be strings or null");
                if(it.value().get_ref<const std::string&>().find_first_of("\r\n")!=std::string::npos)throw std::runtime_error("wire header value contains a line break");
            }
        }
        osv_wire_headers=&patch;
    }
    ~WireHeaderScope(){osv_wire_headers=previous;}
    WireHeaderScope(const WireHeaderScope&)=delete;
    WireHeaderScope& operator=(const WireHeaderScope&)=delete;
};

/** Apply literal header edits before delegating every signing overload to the original official SDK signer; no signature implementation is replaced. */
class HeaderPatchSigner final : public Aws::Client::AWSAuthSigner {
    std::shared_ptr<Aws::Client::AWSAuthSigner> signer;
    static void apply(Aws::Http::HttpRequest& request) {
        if(!osv_wire_headers)return;
        for(auto it=osv_wire_headers->begin();it!=osv_wire_headers->end();++it) {
            if(it.value().is_null())request.DeleteHeader(it.key().c_str());
            else request.SetHeaderValue(it.key().c_str(),it.value().get<std::string>().c_str());
        }
    }
public:
    explicit HeaderPatchSigner(std::shared_ptr<Aws::Client::AWSAuthSigner> original):signer(std::move(original)){}
    const char* GetName() const override{return signer->GetName();}
    bool SignRequest(Aws::Http::HttpRequest& request) const override{apply(request);return signer->SignRequest(request);}
    bool SignRequest(Aws::Http::HttpRequest& request,bool signBody) const override{apply(request);return signer->SignRequest(request,signBody);}
    bool SignRequest(Aws::Http::HttpRequest& request,const char* region,bool signBody) const override{apply(request);return signer->SignRequest(request,region,signBody);}
    bool SignRequest(Aws::Http::HttpRequest& request,const char* region,const char* service,bool signBody) const override{apply(request);return signer->SignRequest(request,region,service,signBody);}
    bool PresignRequest(Aws::Http::HttpRequest& request,long long expiry) const override{apply(request);return signer->PresignRequest(request,expiry);}
    bool PresignRequest(Aws::Http::HttpRequest& request,const char* region,long long expiry) const override{apply(request);return signer->PresignRequest(request,region,expiry);}
    bool PresignRequest(Aws::Http::HttpRequest& request,const char* region,const char* service,long long expiry) const override{apply(request);return signer->PresignRequest(request,region,service,expiry);}
    bool SignEventMessage(Aws::Utils::Event::Message& message,Aws::String& signature) const override{return signer->SignEventMessage(message,signature);}
    void SetClockSkew(const std::chrono::milliseconds& skew) override{signer->SetClockSkew(skew);}
    Aws::Utils::DateTime GetSigningTimestamp() const override{return signer->GetSigningTimestamp();}
};

/** Cache delegating signers for the SDK's provider lifetime, including calls returning raw signer pointers; retain original credentials and added signers. */
class HeaderPatchProvider final : public Aws::Auth::AWSAuthSignerProvider {
    std::shared_ptr<Aws::Auth::AWSAuthSignerProvider> provider;
    mutable std::map<std::string,std::shared_ptr<Aws::Client::AWSAuthSigner>> signers;
    mutable std::mutex mutex;
public:
    explicit HeaderPatchProvider(std::shared_ptr<Aws::Auth::AWSAuthSignerProvider> original):provider(std::move(original)){}
    std::shared_ptr<Aws::Client::AWSAuthSigner> GetSigner(const Aws::String& name) const override {
        std::lock_guard<std::mutex> guard(mutex);auto found=signers.find(name.c_str());
        if(found!=signers.end())return found->second;
        auto original=provider->GetSigner(name);if(!original)return original;
        auto result=std::make_shared<HeaderPatchSigner>(original);signers.emplace(name.c_str(),result);return result;
    }
    void AddSigner(std::shared_ptr<Aws::Client::AWSAuthSigner>& signer) override{std::lock_guard<std::mutex> guard(mutex);provider->AddSigner(signer);signers.erase(signer->GetName());}
    std::shared_ptr<Aws::Auth::AWSCredentialsProvider> GetCredentialsProvider() const override{return provider->GetCredentialsProvider();}
};

/** Use the SDK's protected signer-provider extension on compatibility clients only; all normal S3 serialization, retries and Curl transport stay in the official client. */
class HeaderS3Client final : public Aws::S3::S3Client {
public:
    HeaderS3Client(const Aws::Auth::AWSCredentials& credentials,const Aws::Client::ClientConfiguration& configuration,
        Aws::Client::AWSAuthV4Signer::PayloadSigningPolicy signing,bool virtualAddressing):Aws::S3::S3Client(credentials,configuration,signing,virtualAddressing) {
        m_signerProvider=std::make_shared<HeaderPatchProvider>(m_signerProvider);
    }
};
