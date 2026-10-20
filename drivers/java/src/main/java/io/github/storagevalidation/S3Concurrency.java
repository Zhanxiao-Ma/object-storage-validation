package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.*;
import java.util.concurrent.*;

/**
 * Tests concurrent S3 operations without replacing one shared SDK client with many JVMs.
 * Validates the whole batch before IO, releases real calls together in bounded threads,
 * and preserves each SDK response/error in input order through the existing store.
 */
final class S3Concurrency {
    static Map<String,Object> execute(WorkerMain.Store store,JsonNode input) throws Exception {
        for(String name:Arrays.asList("identity","wire_headers","checksum_when_required"))
            if(input.has(name))throw new IllegalArgumentException("concurrent batch requires the default client: "+name);
        JsonNode requests=input.get("requests");
        if(requests==null || !requests.isArray() || requests.size()<1 || requests.size()>64)
            throw new IllegalArgumentException("concurrent requests must be an array of 1..64 requests");
        for(JsonNode request:requests) {
            if(!request.isObject() || !request.has("op") || !request.get("op").isTextual())
                throw new IllegalArgumentException("concurrent request requires a textual op");
            String op=request.get("op").textValue();
            if(op.isEmpty() || "concurrent_requests".equals(op) || "info".equals(op))
                throw new IllegalArgumentException("invalid concurrent operation: "+op);
            for(String name:Arrays.asList("identity","wire_headers","checksum_when_required"))
                if(request.has(name))throw new IllegalArgumentException("concurrent request requires the default client: "+name);
        }
        CountDownLatch start=new CountDownLatch(1);
        ExecutorService pool=Executors.newFixedThreadPool(requests.size());
        List<Future<Map<String,Object>>> pending=new ArrayList<>();
        try {
            for(JsonNode request:requests)pending.add(pool.submit(()->{
                try {
                    start.await();
                    Map<String,Object> result=new LinkedHashMap<>(store.execute(request));result.put("ok",true);return result;
                } catch(Exception error) {return WorkerMain.failure(error);}
            }));
            start.countDown();
            List<Map<String,Object>> results=new ArrayList<>();
            for(Future<Map<String,Object>> future:pending)results.add(future.get());
            return WorkerMain.one("results",results);
        } finally {
            for(Future<?> future:pending)if(!future.isDone())future.cancel(true);
            pool.shutdownNow();start.countDown();
        }
    }
}
