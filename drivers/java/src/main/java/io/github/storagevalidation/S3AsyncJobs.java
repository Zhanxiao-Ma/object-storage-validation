package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.*;
import java.util.concurrent.*;

/**
 * Preserves source tests whose threads interleave calls on one real S3 client.
 * Starts bounded genuine SDK calls and exposes nonblocking result polling, keeping
 * response capture and SDK elapsed time local to each task instead of forking clients.
 */
final class S3AsyncJobs implements AutoCloseable {
    private final WorkerMain.Store store;
    private final Map<String,Future<Map<String,Object>>> jobs=new LinkedHashMap<>();
    private final ExecutorService pool=Executors.newCachedThreadPool();
    private long next;
    S3AsyncJobs(WorkerMain.Store store){this.store=store;}
    static boolean handles(String op){return "async_start".equals(op)||"async_poll".equals(op);}
    synchronized Map<String,Object> execute(JsonNode q) throws Exception {
        if("async_poll".equals(q.path("op").asText())) {
            if(!q.path("job_id").isTextual())throw new IllegalArgumentException("job_id must be textual");
            String id=q.get("job_id").textValue();Future<Map<String,Object>> job=jobs.get(id);
            if(job==null)throw new IllegalArgumentException("unknown or consumed async job");
            Map<String,Object> result=new LinkedHashMap<>();result.put("done",job.isDone());
            if(job.isDone()){result.put("result",job.get());jobs.remove(id);}
            return result;
        }
        if(jobs.size()>=512)throw new IllegalArgumentException("at most 512 outstanding async jobs");
        JsonNode request=q.get("request");
        if(request==null || !request.isObject() || !request.path("op").isTextual())throw new IllegalArgumentException("async request requires textual op");
        String op=request.get("op").textValue();
        if(op.isEmpty() || handles(op) || op.equals("info") || op.equals("concurrent_requests") || op.equals("select") || op.equals("presign") || op.equals("atomic_stream") || op.startsWith("api.sns.") || op.startsWith("api.s3control.") || op.startsWith("api.iam.") || op.startsWith("api.sts."))throw new IllegalArgumentException("unsupported async operation");
        for(String name:Arrays.asList("identity","wire_headers","wire_query","checksum_when_required"))if(request.has(name))throw new IllegalArgumentException("async requires the existing default S3 client: "+name);
        if(request.has("capture_response_body")&&!request.get("capture_response_body").isBoolean())throw new IllegalArgumentException("capture_response_body must be boolean");
        JsonNode owned=request.deepCopy();String id=Long.toString(++next);
        jobs.put(id,pool.submit(()->{
            long start=System.nanoTime();Map<String,Object> result;
            ResponseBodyCapture.reset();
            try {ResponseBodyCapture.begin(owned);result=new LinkedHashMap<>(store.execute(owned));result.put("ok",true);}
            catch(Exception error){result=WorkerMain.failure(error);}
            ResponseBodyCapture.attach(result);result.put("elapsed_ns",System.nanoTime()-start);return result;
        }));
        return WorkerMain.one("job_id",id);
    }
    public void close(){
        pool.shutdown();
        try {while(!pool.awaitTermination(1,TimeUnit.SECONDS)){} }
        catch(InterruptedException error){pool.shutdownNow();Thread.currentThread().interrupt();}
        jobs.clear();
    }
}
