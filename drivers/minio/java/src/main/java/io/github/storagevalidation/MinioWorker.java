package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.minio.*;
import io.minio.credentials.StaticProvider;
import io.minio.errors.ErrorResponseException;
import io.minio.messages.Part;
import java.io.*;
import java.net.URI;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.CompletionException;
import java.util.concurrent.TimeUnit;
import okhttp3.OkHttpClient;

/**
 * Measures the official MinIO Java SDK through the same persistent JSONL protocol as AWS.
 * Public single-request and multipart APIs preserve request boundaries; full response streams
 * are consumed before stopping the request clock. Dependencies stay separate from the AWS worker.
 */
public final class MinioWorker {
    private static final ObjectMapper JSON=new ObjectMapper();
    private final MinioAsyncClient client;
    private final String bucket=required("BUCKET");
    private final String region=env("REGION","us-east-1");
    private static String env(String key,String fallback) {return System.getenv().getOrDefault("OSV_"+key,fallback);}
    private static String required(String key) {String v=env(key,"");if(v.isEmpty())throw new IllegalArgumentException("missing OSV_"+key);return v;}
    private static String text(JsonNode q,String key) {return q.path(key).asText();}
    private MinioWorker() {
        if(!env("RETRIES","0").equals("0"))throw new IllegalArgumentException("MinIO performance worker requires OSV_RETRIES=0");
        URI endpoint=URI.create(required("ENDPOINT"));
        if(!Arrays.asList("http","https").contains(endpoint.getScheme()) || endpoint.getHost()==null || endpoint.getUserInfo()!=null)
            throw new IllegalArgumentException("invalid S3 endpoint");
        int connections=Integer.parseInt(env("CONNECTIONS","16"));
        OkHttpClient http=new OkHttpClient.Builder().retryOnConnectionFailure(false)
            .connectionPool(new okhttp3.ConnectionPool(connections,5,TimeUnit.MINUTES))
            .callTimeout(60,TimeUnit.SECONDS).build();
        client=MinioAsyncClient.builder().endpoint(endpoint.toString()).region(region)
            .credentialsProvider(new StaticProvider(required("ACCESS_KEY"),required("SECRET_KEY"),env("SESSION_TOKEN","").isEmpty()?null:env("SESSION_TOKEN","")))
            .httpClient(http,true).build();
        client.setRetry(null,0L,0);
        if(Boolean.parseBoolean(env("PATH_STYLE","true")))client.disableVirtualStyleEndpoint();else client.enableVirtualStyleEndpoint();
    }
    private Map<String,Object> execute(JsonNode q) throws Exception {
        String op=text(q,"op"),key=text(q,"key");Map<String,Object> out=new LinkedHashMap<>();
        switch(op) {
            case "info":
                out.put("language","java");out.put("api","s3");out.put("backend","minio");out.put("sdk_version",Utils.MinioProperties.INSTANCE.getVersion());
                out.put("protocol_version",1);out.put("capabilities",Arrays.asList("put","get","head","list","delete","mp_create","mp_part","mp_complete","mp_abort"));
                out.put("endpoint",env("ENDPOINT",""));out.put("region",region);out.put("bucket",bucket);out.put("retries","0");out.put("connections_per_worker",env("CONNECTIONS","16"));
                out.put("measurement","SDK operation and payload file IO; full response consumed; excludes JSONL");break;
            case "put":
                {
                    byte[] data=Files.readAllBytes(Path.of(text(q,"body_path")));
                    out.put("etag",client.putObject(PutObjectAPIArgs.builder().bucket(bucket).object(key).data(data,data.length).contentType("application/octet-stream").build()).join().etag());
                }break;
            case "get":
                GetObjectArgs.Builder args=GetObjectArgs.builder().bucket(bucket).object(key);
                if(q.has("offset"))args.offset(q.get("offset").asLong()).length(q.get("length").asLong());
                try(InputStream input=client.getObject(args.build()).join();OutputStream output=Files.newOutputStream(Path.of(text(q,"output_path")))) {input.transferTo(output);}break;
            case "head":out.put("size",client.statObject(StatObjectArgs.builder().bucket(bucket).object(key).build()).join().size());break;
            case "list":
                List<String> keys=new ArrayList<>();
                for(Result<io.minio.messages.Item> result:client.listObjects(ListObjectsArgs.builder().bucket(bucket).prefix(text(q,"prefix")).recursive(true).maxKeys(1000).build()))keys.add(result.get().objectName());
                Collections.sort(keys);out.put("keys",keys);break;
            case "delete":client.removeObject(RemoveObjectArgs.builder().bucket(bucket).object(key).build()).join();break;
            case "mp_create":out.put("upload_id",client.createMultipartUpload(CreateMultipartUploadArgs.builder().bucket(bucket).object(key).build()).join().result().uploadId());break;
            case "mp_part":
                {
                    byte[] data=Files.readAllBytes(Path.of(text(q,"body_path")));
                    out.put("etag",client.uploadPart(UploadPartArgs.builder().bucket(bucket).object(key).uploadId(text(q,"upload_id")).partNumber(q.path("part_number").asInt()).data(data,data.length).build()).join().part().etag());
                }break;
            case "mp_complete":
                List<Part> parts=new ArrayList<>();for(JsonNode part:q.path("parts"))parts.add(new Part(part.path("part_number").asInt(),text(part,"etag")));
                out.put("etag",client.completeMultipartUpload(CompleteMultipartUploadArgs.builder().bucket(bucket).object(key).uploadId(text(q,"upload_id")).parts(parts.toArray(new Part[0])).maxRetries(0).build()).join().etag());break;
            case "mp_abort":client.abortMultipartUpload(AbortMultipartUploadArgs.builder().bucket(bucket).object(key).uploadId(text(q,"upload_id")).build()).join();break;
            default:throw new IllegalArgumentException("unsupported operation: "+op);
        }
        return out;
    }
    public static void main(String[] args) throws Exception {
        PrintStream protocol=System.out;System.setOut(System.err);MinioWorker worker=new MinioWorker();
        try(BufferedReader input=new BufferedReader(new InputStreamReader(System.in))) {
            for(String line;(line=input.readLine())!=null;) {
                Map<String,Object> result=new LinkedHashMap<>();long start=System.nanoTime();
                try {JsonNode request=JSON.readTree(line);start=System.nanoTime();result.putAll(worker.execute(request));result.put("ok",true);}
                catch(Exception error) {
                    Throwable cause=error;while(cause instanceof CompletionException && cause.getCause()!=null)cause=cause.getCause();
                    result.put("ok",false);result.put("error",cause.getClass().getSimpleName()+": "+cause.getMessage());result.put("error_kind","sdk_error");
                    if(cause instanceof ErrorResponseException) {ErrorResponseException e=(ErrorResponseException)cause;result.put("status_code",e.response().code());result.put("error_code",e.errorResponse().code());if(e.response().code()==404)result.put("error_kind","not_found");}
                }
                result.put("elapsed_ns",System.nanoTime()-start);protocol.println(JSON.writeValueAsString(result));
            }
        }finally {worker.client.close();}
    }
}
