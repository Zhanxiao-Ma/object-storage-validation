#pragma once
#include <aws/core/utils/StringUtils.h>

inline thread_local const json* osv_wire_query=nullptr;

/** Scope validated source query edits to one actual SDK request and restore them on failure/cleanup. */
class WireQueryScope {
    const json* previous;
public:
    explicit WireQueryScope(const json& input):previous(osv_wire_query) {
        if(!input.is_object())throw std::runtime_error("wire_query must be an object");
        for(auto item=input.begin();item!=input.end();++item) {
            if(item.key().empty())throw std::runtime_error("invalid query name");
            for(unsigned char ch:item.key())if(ch<32 || ch==127)throw std::runtime_error("invalid query name");
            if(!item.value().is_null() && !item.value().is_string() && !(item.value().is_boolean() && item.value().get<bool>()))throw std::runtime_error("query values must be strings, true flags or null removals");
        }
        osv_wire_query=&input;
    }
    ~WireQueryScope(){osv_wire_query=previous;}
    WireQueryScope(const WireQueryScope&)=delete;
    WireQueryScope& operator=(const WireQueryScope&)=delete;
};

inline void apply_wire_query(Aws::Http::HttpRequest& request) {
    if(!osv_wire_query)return;
    auto& uri=request.GetUri();std::string original=uri.GetQueryString().c_str(),result;
    if(!original.empty() && original[0]=='?')original.erase(0,1);
    auto append=[&result](const std::string& value){if(!result.empty())result+='&';result+=value;};
    for(size_t start=0;start<original.size();) {
        auto end=original.find('&',start);if(end==std::string::npos)end=original.size();
        auto token=original.substr(start,end-start),name=token.substr(0,token.find('='));
        if(!osv_wire_query->contains(std::string(Aws::Utils::StringUtils::URLDecode(name.c_str()).c_str())))append(token);
        start=end+1;
    }
    for(auto item=osv_wire_query->begin();item!=osv_wire_query->end();++item)if(!item.value().is_null()) {
        std::string part=Aws::Utils::StringUtils::URLEncode(item.key().c_str()).c_str();
        if(item.value().is_string())part+='='+std::string(Aws::Utils::StringUtils::URLEncode(item.value().get_ref<const std::string&>().c_str()).c_str());
        else part+='='; // Prevent the pinned SDK URI canonicalizer from treating a bare flag as its own value.
        append(part);
    }
    uri.SetQueryString(result.c_str());
}
