#pragma once
#include <cstdio>
#include <cstdlib>
#include <stdexcept>
#include <string>
#include <unistd.h>

/**
 * Prevent libcurl's default stdin body reader from consuming or blocking on JSONL control messages.
 * Read the protocol from a private duplicate of the input pipe and give SDK code an EOF-only stdin;
 * request models, headers, signer and official HTTP transport are left unchanged.
 */
class ProtocolInput final {
    FILE* input=nullptr;
    char* buffer=nullptr;
    size_t capacity=0;
public:
    ProtocolInput() {
        int descriptor=dup(STDIN_FILENO);
        if(descriptor<0)throw std::runtime_error("cannot isolate protocol input");
        input=fdopen(descriptor,"r");
        if(!input){close(descriptor);throw std::runtime_error("cannot open protocol input");}
        if(!freopen("/dev/null","r",stdin)){fclose(input);input=nullptr;throw std::runtime_error("cannot isolate SDK stdin");}
    }
    ~ProtocolInput(){if(input)fclose(input);free(buffer);}
    bool read(std::string& line) {
        auto size=::getline(&buffer,&capacity,input);
        if(size<0){if(ferror(input))throw std::runtime_error("protocol input read failed");return false;}
        line.assign(buffer,static_cast<size_t>(size));return true;
    }
    ProtocolInput(const ProtocolInput&)=delete;
    ProtocolInput& operator=(const ProtocolInput&)=delete;
};
