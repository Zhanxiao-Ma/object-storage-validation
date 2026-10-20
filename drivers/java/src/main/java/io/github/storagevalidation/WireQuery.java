package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.*;
import software.amazon.awssdk.core.interceptor.*;
import software.amazon.awssdk.http.SdkHttpRequest;

/**
 * Preserves source before-signing query edits that typed SDK parameters cannot express.
 * Scopes validated replacements, bare flags and removals to one compatibility request;
 * the official SDK encodes the final query and signs/transports it normally.
 */
final class WireQuery implements ExecutionInterceptor {
    private static final ThreadLocal<Map<String,JsonNode>> ACTIVE=new ThreadLocal<>();
    /** Restore the previous query context on every exit so cleanup cannot inherit a source mutation. */
    static final class Scope implements AutoCloseable {
        private final Map<String,JsonNode> previous;
        Scope(JsonNode input) {
            if(!input.isObject())throw new IllegalArgumentException("wire_query must be an object");
            Map<String,JsonNode> patch=new LinkedHashMap<>();
            input.fields().forEachRemaining(entry->{
                if(entry.getKey().isEmpty() || entry.getKey().codePoints().anyMatch(Character::isISOControl))throw new IllegalArgumentException("invalid query name");
                JsonNode value=entry.getValue();
                if(!value.isNull() && !value.isTextual() && !(value.isBoolean() && value.booleanValue()))throw new IllegalArgumentException("query values must be strings, true flags or null removals");
                patch.put(entry.getKey(),value);
            });
            previous=ACTIVE.get();ACTIVE.set(Collections.unmodifiableMap(patch));
        }
        public void close(){if(previous==null)ACTIVE.remove();else ACTIVE.set(previous);}
    }
    public SdkHttpRequest modifyHttpRequest(Context.ModifyHttpRequest context,ExecutionAttributes attributes) {
        Map<String,JsonNode> patch=ACTIVE.get();if(patch==null)return context.httpRequest();
        SdkHttpRequest.Builder builder=context.httpRequest().toBuilder();
        patch.forEach((name,value)->{
            builder.removeQueryParameter(name);
            if(!value.isNull())builder.putRawQueryParameter(name,Collections.singletonList(value.isBoolean()?null:value.textValue()));
        });
        return builder.build();
    }
}
