package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.*;
import software.amazon.awssdk.core.interceptor.*;
import software.amazon.awssdk.http.SdkHttpRequest;

/**
 * Exercises original Ceph before-signing header changes through the official SDK.
 * A scoped thread-local patch feeds a public execution interceptor on a separate
 * compatibility client; signing, transport and subsequent SDK normalization remain authoritative.
 */
final class WireHeaders implements ExecutionInterceptor {
    private static final ThreadLocal<Map<String,String>> ACTIVE=new ThreadLocal<>();

    /** Restore the previous request context on success or failure, preventing patches from leaking into cleanup or another identity. */
    static final class Scope implements AutoCloseable {
        private final Map<String,String> previous;
        Scope(JsonNode value) {
            if(!value.isObject())throw new IllegalArgumentException("wire_headers must be an object");
            Map<String,String> patch=new LinkedHashMap<>();
            value.fields().forEachRemaining(entry->{
                String name=entry.getKey().toLowerCase(Locale.ROOT);
                if(!name.matches("[!#$%&'*+.^_`|~0-9a-z-]+") || patch.containsKey(name))throw new IllegalArgumentException("invalid or duplicate header name");
                JsonNode input=entry.getValue();
                if(!input.isNull() && !input.isTextual())throw new IllegalArgumentException("wire header values must be strings or null");
                String text=input.isNull()?null:input.textValue();
                if(text!=null && (text.contains("\r")||text.contains("\n")))throw new IllegalArgumentException("wire header value contains a line break");
                patch.put(name,text);
            });
            previous=ACTIVE.get();ACTIVE.set(Collections.unmodifiableMap(patch));
        }
        public void close() { if(previous==null)ACTIVE.remove();else ACTIVE.set(previous); }
    }

    public SdkHttpRequest modifyHttpRequest(Context.ModifyHttpRequest context,ExecutionAttributes attributes) {
        Map<String,String> patch=ACTIVE.get();
        if(patch==null)return context.httpRequest();
        SdkHttpRequest.Builder builder=context.httpRequest().toBuilder();
        patch.forEach((name,value)->{builder.removeHeader(name);if(value!=null)builder.putHeader(name,value);});
        return builder.build();
    }
}
