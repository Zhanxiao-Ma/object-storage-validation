package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.*;
import java.util.*;
import software.amazon.awssdk.awscore.AwsResponse;
import software.amazon.awssdk.awscore.exception.AwsServiceException;
import software.amazon.awssdk.core.interceptor.*;

/**
 * Exposes bounded genuine HTTP response bytes for source-level protocol assertions.
 * An opt-in interceptor reads the actual response before SDK unmarshalling and
 * supplies identical bytes back; request IDs come only from official SDK metadata.
 * Per-RPC state is cleared before and after execution, including failed requests.
 */
final class ResponseBodyCapture implements ExecutionInterceptor {
    private static final int LIMIT=512*1024;
    private static final ThreadLocal<State> ACTIVE=new ThreadLocal<>();
    /** Keep one RPC's actual final response and typed SDK request ID without sharing mutable capture state across worker threads. */
    private static final class State { byte[] body;String requestId;String error; }
    static void reset(){ACTIVE.remove();}
    static void begin(JsonNode request) {
        if(!request.has("capture_response_body"))return;
        if(!request.get("capture_response_body").isBoolean())throw new IllegalArgumentException("capture_response_body must be boolean");
        if(request.get("capture_response_body").booleanValue()) {
            if("concurrent_requests".equals(request.path("op").asText()))throw new IllegalArgumentException("body capture requires an individual SDK request");
            ACTIVE.set(new State());
        }
    }
    static void attach(Map<String,Object> result) {
        State state=ACTIVE.get();
        try {
            if(state==null)return;
            if(state.body!=null)result.put("response_body_b64",Base64.getEncoder().encodeToString(state.body));
            if(state.requestId!=null)result.put("request_id",state.requestId);
            if(state.error!=null)result.put("response_body_capture_error",state.error);
        } finally {reset();}
    }
    public Optional<InputStream> modifyHttpResponseContent(Context.ModifyHttpResponse context,ExecutionAttributes attributes) {
        State state=ACTIVE.get();if(state==null)return context.responseBody();
        state.body=null;state.requestId=null;state.error=null;
        if(!context.responseBody().isPresent()){state.body=new byte[0];return context.responseBody();}
        try(InputStream original=context.responseBody().get()) {
            byte[] bytes=original.readNBytes(LIMIT+1);
            if(bytes.length>LIMIT){state.error="actual response exceeds 512 KiB capture limit";throw new IllegalStateException(state.error);}
            state.body=bytes;return Optional.of(new ByteArrayInputStream(bytes));
        } catch(IOException error) {state.error="cannot capture actual response body";throw new UncheckedIOException(state.error,error);}
    }
    public void afterExecution(Context.AfterExecution context,ExecutionAttributes attributes) {
        State state=ACTIVE.get();
        if(state!=null && context.response() instanceof AwsResponse)state.requestId=((AwsResponse)context.response()).responseMetadata().requestId();
    }
    public void onExecutionFailure(Context.FailedExecution context,ExecutionAttributes attributes) {
        State state=ACTIVE.get();
        if(state!=null && context.exception() instanceof AwsServiceException)state.requestId=((AwsServiceException)context.exception()).requestId();
    }
}
