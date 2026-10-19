package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.*;
import java.nio.file.*;
import java.util.*;

/**
 * Runs real Java SDK operations without mixing JVM startup with request latency.
 * A persistent JSONL protocol delegates each operation to the selected vendor adapter;
 * payload files keep large bodies out of the control channel.
 */
public final class WorkerMain {
    static final ObjectMapper JSON = new ObjectMapper();
    static String env(String key, String fallback) { return System.getenv().getOrDefault("OSV_"+key, fallback); }
    static String required(String key) {
        String value = env(key, "");
        if (value.isEmpty()) throw new IllegalArgumentException("missing OSV_"+key);
        return value;
    }
    static String text(JsonNode node, String key) { return node.path(key).asText(); }
    static String sdkVersion(String provider) throws IOException {
        String path;
        switch(provider) {
            case "s3": path="software.amazon.awssdk/s3"; break;
            case "oss": path="com.aliyun.oss/aliyun-sdk-oss"; break;
            case "obs": path="com.huaweicloud/esdk-obs-java"; break;
            case "cos": path="com.qcloud/cos_api"; break;
            case "tos": path="com.volcengine/ve-tos-java-sdk"; break;
            default: throw new IllegalArgumentException(provider);
        }
        Properties properties=new Properties();
        try(InputStream input=WorkerMain.class.getResourceAsStream("/META-INF/maven/"+path+"/pom.properties")) {
            if(input==null) throw new IOException("SDK build version unavailable");
            properties.load(input);
        }
        return properties.getProperty("version");
    }

    /**
     * Separates vendor requests from common measurement and control handling.
     * Every adapter returns actual SDK results; errors stay visible to the harness.
     */
    interface Store extends AutoCloseable {
        Map<String,Object> execute(JsonNode request) throws Exception;
        @Override void close();
    }

    public static void main(String[] args) throws Exception {
        // Some vendor SDKs log to stdout. Keep the JSONL stream private and redirect logs.
        PrintStream protocol = System.out;
        System.setOut(System.err);
        String provider = args.length == 0 ? "s3" : args[0];
        try (Store store = create(provider); BufferedReader input = new BufferedReader(new InputStreamReader(System.in))) {
            for (String line; (line = input.readLine()) != null;) {
                long start = System.nanoTime();
                Map<String,Object> result = new LinkedHashMap<>();
                try {
                    JsonNode request = JSON.readTree(line);
                    if ("info".equals(text(request,"op"))) {
                        result.put("language", "java"); result.put("backend", provider);
                        result.put("api", "s3".equals(provider)?"s3":"native");
                        result.put("sdk_version", sdkVersion(provider));
                        result.put("protocol_version", 1);
                        result.put("capabilities", Arrays.asList("put", "get", "head", "list", "delete", "mp_create", "mp_part", "mp_complete", "mp_abort"));
                        result.put("endpoint",env("ENDPOINT","")); result.put("region",env("REGION",""));
                        result.put("bucket",env("BUCKET","")); result.put("retries",env("RETRIES","0"));
                        result.put("connections_per_worker",env("CONNECTIONS","16"));
                        result.put("runtime", System.getProperty("java.version"));
                        result.put("measurement", "operation including local payload IO; excludes JSONL serialization");
                    } else result.putAll(store.execute(request));
                    result.put("ok", true);
                } catch (Exception error) {
                    result.put("ok", false);
                    result.put("error_kind", notFound(error) ? "not_found" : "sdk_error");
                    result.put("error", error.getClass().getSimpleName()+": "+error.getMessage());
                }
                result.put("elapsed_ns", System.nanoTime()-start);
                protocol.println(JSON.writeValueAsString(result));
            }
        }
    }
    static boolean notFound(Exception error) {
        if (error instanceof software.amazon.awssdk.services.s3.model.S3Exception)
            return ((software.amazon.awssdk.services.s3.model.S3Exception)error).statusCode()==404;
        if (error instanceof com.aliyun.oss.OSSException)
            return "NoSuchKey".equals(((com.aliyun.oss.OSSException)error).getErrorCode());
        if (error instanceof com.obs.services.exception.ObsException)
            return ((com.obs.services.exception.ObsException)error).getResponseCode()==404;
        if (error instanceof com.volcengine.tos.TosServerException)
            return ((com.volcengine.tos.TosServerException)error).getStatusCode()==404;
        if (error instanceof com.qcloud.cos.exception.CosServiceException)
            return ((com.qcloud.cos.exception.CosServiceException)error).getStatusCode()==404;
        return error instanceof FileNotFoundException;
    }
    static Store create(String provider) {
        switch (provider) {
            case "s3": return new AwsStore();
            case "oss": return new OssStore();
            case "obs": return new ObsStore();
            case "tos": return new TosStore();
            case "cos": return new CosStore();
            default: throw new IllegalArgumentException("supported: s3, oss, obs, tos, cos");
        }
    }
    static Map<String,Object> one(String key, Object value) { return Collections.singletonMap(key,value); }
    static Map<String,Object> empty() { return Collections.emptyMap(); }

    /**
     * Exercises AWS SDK v2 S3 rather than a substitute HTTP implementation.
     * Explicit connection, retry, region and addressing settings make runs auditable.
     */
    static final class AwsStore implements Store {
        final software.amazon.awssdk.services.s3.S3Client client;
        final String bucket = required("BUCKET");
        AwsStore() {
            software.amazon.awssdk.auth.credentials.AwsCredentials credentials = env("SESSION_TOKEN", "").isEmpty()
                ? software.amazon.awssdk.auth.credentials.AwsBasicCredentials.create(required("ACCESS_KEY"),required("SECRET_KEY"))
                : software.amazon.awssdk.auth.credentials.AwsSessionCredentials.create(required("ACCESS_KEY"),required("SECRET_KEY"),required("SESSION_TOKEN"));
            client = software.amazon.awssdk.services.s3.S3Client.builder()
                .endpointOverride(java.net.URI.create(required("ENDPOINT")))
                .region(software.amazon.awssdk.regions.Region.of(required("REGION")))
                .credentialsProvider(software.amazon.awssdk.auth.credentials.StaticCredentialsProvider.create(credentials))
                .serviceConfiguration(software.amazon.awssdk.services.s3.S3Configuration.builder()
                    .pathStyleAccessEnabled(Boolean.parseBoolean(env("PATH_STYLE","false"))).build())
                .overrideConfiguration(c -> c.apiCallTimeout(java.time.Duration.ofSeconds(30))
                    .retryPolicy(software.amazon.awssdk.core.retry.RetryPolicy.builder().numRetries(Integer.parseInt(env("RETRIES","0"))).build()))
                .httpClientBuilder(software.amazon.awssdk.http.apache.ApacheHttpClient.builder()
                    .maxConnections(Integer.parseInt(env("CONNECTIONS","16"))))
                .build();
        }
        public Map<String,Object> execute(JsonNode q) throws Exception {
            String key=text(q,"key"), op=text(q,"op");
            switch(op) {
            case "put":
                client.putObject(software.amazon.awssdk.services.s3.model.PutObjectRequest.builder().bucket(bucket).key(key).build(),
                    software.amazon.awssdk.core.sync.RequestBody.fromFile(Paths.get(text(q,"body_path")))); return empty();
            case "get": {
                software.amazon.awssdk.services.s3.model.GetObjectRequest.Builder b=software.amazon.awssdk.services.s3.model.GetObjectRequest.builder().bucket(bucket).key(key);
                if(q.has("offset")) b.range("bytes="+q.get("offset").asLong()+"-"+(q.get("offset").asLong()+q.get("length").asLong()-1));
                try(InputStream in=client.getObject(b.build())) { Files.copy(in,Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING); } return empty();
            }
            case "head": return one("size",client.headObject(b->b.bucket(bucket).key(key)).contentLength());
            case "delete": client.deleteObject(b->b.bucket(bucket).key(key)); return empty();
            case "list": {
                List<String> keys=new ArrayList<>();
                for(software.amazon.awssdk.services.s3.model.ListObjectsV2Response page:client.listObjectsV2Paginator(b->b.bucket(bucket).prefix(text(q,"prefix"))))
                    for(software.amazon.awssdk.services.s3.model.S3Object object:page.contents()) keys.add(object.key());
                return one("keys",keys);
            }
            case "mp_create": return one("upload_id",client.createMultipartUpload(b->b.bucket(bucket).key(key)).uploadId());
            case "mp_part": return one("etag",client.uploadPart(b->b.bucket(bucket).key(key).uploadId(text(q,"upload_id")).partNumber(q.get("part_number").asInt()),
                software.amazon.awssdk.core.sync.RequestBody.fromFile(Paths.get(text(q,"body_path")))).eTag());
            case "mp_complete": {
                List<software.amazon.awssdk.services.s3.model.CompletedPart> parts=new ArrayList<>();
                for(JsonNode part:q.get("parts")) parts.add(software.amazon.awssdk.services.s3.model.CompletedPart.builder().partNumber(part.get("part_number").asInt()).eTag(text(part,"etag")).build());
                client.completeMultipartUpload(b->b.bucket(bucket).key(key).uploadId(text(q,"upload_id")).multipartUpload(m->m.parts(parts))); return empty();
            }
            case "mp_abort": client.abortMultipartUpload(b->b.bucket(bucket).key(key).uploadId(text(q,"upload_id"))); return empty();
            default: throw new UnsupportedOperationException(op);
            }
        }
        public void close() { client.close(); }
    }

    /**
     * Measures OSS native requests through the official OSS Java SDK.
     * Matching operations preserve full response bodies and pagination semantics.
     */
    static final class OssStore implements Store {
        final com.aliyun.oss.OSS client;
        final String bucket=required("BUCKET");
        OssStore() {
            com.aliyun.oss.ClientBuilderConfiguration configuration=new com.aliyun.oss.ClientBuilderConfiguration();
            configuration.setMaxErrorRetry(Integer.parseInt(env("RETRIES","0")));
            configuration.setMaxConnections(Integer.parseInt(env("CONNECTIONS","16")));
            client=new com.aliyun.oss.OSSClientBuilder().build(required("ENDPOINT"),
                new com.aliyun.oss.common.auth.DefaultCredentialProvider(required("ACCESS_KEY"),required("SECRET_KEY"),env("SESSION_TOKEN",null)),configuration);
        }
        public Map<String,Object> execute(JsonNode q) throws Exception {
            String key=text(q,"key"), op=text(q,"op");
            switch(op) {
            case "put": client.putObject(bucket,key,new File(text(q,"body_path"))); return empty();
            case "get": {
                com.aliyun.oss.model.GetObjectRequest request=new com.aliyun.oss.model.GetObjectRequest(bucket,key);
                if(q.has("offset")) request.setRange(q.get("offset").asLong(),q.get("offset").asLong()+q.get("length").asLong()-1);
                try(com.aliyun.oss.model.OSSObject object=client.getObject(request)) { Files.copy(object.getObjectContent(),Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING); } return empty();
            }
            case "head": return one("size",client.getObjectMetadata(bucket,key).getContentLength());
            case "delete": client.deleteObject(bucket,key); return empty();
            case "list": {
                List<String> keys=new ArrayList<>();
                com.aliyun.oss.model.ListObjectsV2Request request=new com.aliyun.oss.model.ListObjectsV2Request(bucket).withPrefix(text(q,"prefix"));
                com.aliyun.oss.model.ListObjectsV2Result page;
                do { page=client.listObjectsV2(request); for(com.aliyun.oss.model.OSSObjectSummary object:page.getObjectSummaries()) keys.add(object.getKey()); request.setContinuationToken(page.getNextContinuationToken()); } while(page.isTruncated());
                return one("keys",keys);
            }
            case "mp_create": return one("upload_id",client.initiateMultipartUpload(new com.aliyun.oss.model.InitiateMultipartUploadRequest(bucket,key)).getUploadId());
            case "mp_part": {
                com.aliyun.oss.model.UploadPartRequest request=new com.aliyun.oss.model.UploadPartRequest();
                request.setBucketName(bucket); request.setKey(key); request.setUploadId(text(q,"upload_id")); request.setPartNumber(q.get("part_number").asInt());
                File file=new File(text(q,"body_path")); request.setPartSize(file.length());
                try(InputStream in=new FileInputStream(file)) { request.setInputStream(in); return one("etag",client.uploadPart(request).getETag()); }
            }
            case "mp_complete": {
                List<com.aliyun.oss.model.PartETag> parts=new ArrayList<>();
                for(JsonNode part:q.get("parts")) parts.add(new com.aliyun.oss.model.PartETag(part.get("part_number").asInt(),text(part,"etag")));
                client.completeMultipartUpload(new com.aliyun.oss.model.CompleteMultipartUploadRequest(bucket,key,text(q,"upload_id"),parts)); return empty();
            }
            case "mp_abort": client.abortMultipartUpload(new com.aliyun.oss.model.AbortMultipartUploadRequest(bucket,key,text(q,"upload_id"))); return empty();
            default: throw new UnsupportedOperationException(op);
            }
        }
        public void close() { client.shutdown(); }
    }

    /**
     * Exercises TOS native Java requests without routing them through S3 compatibility.
     * Equivalent low-level operations make SDK comparisons auditable and repeatable.
     */
    static final class TosStore implements Store {
        final com.volcengine.tos.TOSV2 client;
        final String bucket=required("BUCKET");
        TosStore() {
            com.volcengine.tos.transport.TransportConfig transport=new com.volcengine.tos.transport.TransportConfig();
            transport.setMaxRetryCount(Integer.parseInt(env("RETRIES","0")));
            transport.setMaxConnections(Integer.parseInt(env("CONNECTIONS","16")));
            client=new com.volcengine.tos.TOSV2ClientBuilder().build(required("REGION"),required("ENDPOINT"),
                required("ACCESS_KEY"),required("SECRET_KEY"),env("SESSION_TOKEN",null),transport);
        }
        public Map<String,Object> execute(JsonNode q) throws Exception {
            String key=text(q,"key"),op=text(q,"op");
            switch(op) {
            case "put": {
                Path file=Paths.get(text(q,"body_path"));
                try(InputStream in=Files.newInputStream(file)) {
                    client.putObject(new com.volcengine.tos.model.object.PutObjectInput().setBucket(bucket).setKey(key).setContent(in).setContentLength(Files.size(file)));
                } return empty();
            }
            case "get": {
                com.volcengine.tos.model.object.GetObjectV2Input request=new com.volcengine.tos.model.object.GetObjectV2Input().setBucket(bucket).setKey(key);
                if(q.has("offset")) request.setRange("bytes="+q.get("offset").asLong()+"-"+(q.get("offset").asLong()+q.get("length").asLong()-1));
                try(InputStream in=client.getObject(request).getContent()) { Files.copy(in,Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING); } return empty();
            }
            case "head": return one("size",client.headObject(new com.volcengine.tos.model.object.HeadObjectV2Input().setBucket(bucket).setKey(key)).getContentLength());
            case "delete": client.deleteObject(new com.volcengine.tos.model.object.DeleteObjectInput().setBucket(bucket).setKey(key)); return empty();
            case "list": {
                List<String> keys=new ArrayList<>();
                com.volcengine.tos.model.object.ListObjectsType2Input request=new com.volcengine.tos.model.object.ListObjectsType2Input().setBucket(bucket).setPrefix(text(q,"prefix"));
                com.volcengine.tos.model.object.ListObjectsType2Output page;
                do { page=client.listObjectsType2(request); for(com.volcengine.tos.model.object.ListedObjectV2 object:page.getContents()) keys.add(object.getKey()); request.setContinuationToken(page.getNextContinuationToken()); } while(page.isTruncated());
                return one("keys",keys);
            }
            case "mp_create": return one("upload_id",client.createMultipartUpload(new com.volcengine.tos.model.object.CreateMultipartUploadInput().setBucket(bucket).setKey(key)).getUploadID());
            case "mp_part": {
                Path file=Paths.get(text(q,"body_path"));
                try(InputStream in=Files.newInputStream(file)) { return one("etag",client.uploadPart(new com.volcengine.tos.model.object.UploadPartV2Input().setBucket(bucket).setKey(key).setUploadID(text(q,"upload_id")).setPartNumber(q.get("part_number").asInt()).setContent(in).setContentLength(Files.size(file))).getEtag()); }
            }
            case "mp_complete": {
                List<com.volcengine.tos.model.object.UploadedPartV2> parts=new ArrayList<>();
                for(JsonNode part:q.get("parts")) parts.add(new com.volcengine.tos.model.object.UploadedPartV2().setPartNumber(part.get("part_number").asInt()).setEtag(text(part,"etag")));
                client.completeMultipartUpload(new com.volcengine.tos.model.object.CompleteMultipartUploadV2Input().setBucket(bucket).setKey(key).setUploadID(text(q,"upload_id")).setUploadedParts(parts)); return empty();
            }
            case "mp_abort": client.abortMultipartUpload(new com.volcengine.tos.model.object.AbortMultipartUploadInput().setBucket(bucket).setKey(key).setUploadID(text(q,"upload_id"))); return empty();
            default: throw new UnsupportedOperationException(op);
            }
        }
        public void close() { try { client.close(); } catch(IOException error) { throw new UncheckedIOException(error); } }
    }

    /**
     * Exercises OBS native Java operations for comparisons on the same storage.
     * The adapter translates operation inputs, not service responses or measured errors.
     */
    static final class ObsStore implements Store {
        final com.obs.services.ObsClient client;
        final String bucket=required("BUCKET");
        ObsStore() {
            com.obs.services.ObsConfiguration configuration=new com.obs.services.ObsConfiguration();
            configuration.setEndPoint(required("ENDPOINT"));
            configuration.setMaxErrorRetry(Integer.parseInt(env("RETRIES","0")));
            configuration.setMaxConnections(Integer.parseInt(env("CONNECTIONS","16")));
            client=new com.obs.services.ObsClient(required("ACCESS_KEY"),required("SECRET_KEY"),env("SESSION_TOKEN",null),configuration);
        }
        public Map<String,Object> execute(JsonNode q) throws Exception {
            String key=text(q,"key"),op=text(q,"op");
            switch(op) {
            case "put": client.putObject(bucket,key,new File(text(q,"body_path"))); return empty();
            case "get": {
                com.obs.services.model.GetObjectRequest request=new com.obs.services.model.GetObjectRequest(bucket,key);
                if(q.has("offset")) { request.setRangeStart(q.get("offset").asLong()); request.setRangeEnd(q.get("offset").asLong()+q.get("length").asLong()-1); }
                try(InputStream in=client.getObject(request).getObjectContent()) { Files.copy(in,Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING); } return empty();
            }
            case "head": return one("size",client.getObjectMetadata(bucket,key).getContentLength());
            case "delete": client.deleteObject(bucket,key); return empty();
            case "list": {
                List<String> keys=new ArrayList<>(); com.obs.services.model.ListObjectsRequest request=new com.obs.services.model.ListObjectsRequest(bucket); request.setPrefix(text(q,"prefix"));
                com.obs.services.model.ObjectListing page;
                do { page=client.listObjects(request); for(com.obs.services.model.ObsObject object:page.getObjects()) keys.add(object.getObjectKey()); request.setMarker(page.getNextMarker()); } while(page.isTruncated());
                return one("keys",keys);
            }
            case "mp_create": return one("upload_id",client.initiateMultipartUpload(new com.obs.services.model.InitiateMultipartUploadRequest(bucket,key)).getUploadId());
            case "mp_part": {
                com.obs.services.model.UploadPartRequest request=new com.obs.services.model.UploadPartRequest(); request.setBucketName(bucket); request.setObjectKey(key); request.setUploadId(text(q,"upload_id")); request.setPartNumber(q.get("part_number").asInt());
                File file=new File(text(q,"body_path")); request.setPartSize(file.length());
                try(InputStream in=new FileInputStream(file)) { request.setInput(in); return one("etag",client.uploadPart(request).getEtag()); }
            }
            case "mp_complete": {
                List<com.obs.services.model.PartEtag> parts=new ArrayList<>(); for(JsonNode part:q.get("parts")) parts.add(new com.obs.services.model.PartEtag(text(part,"etag"),part.get("part_number").asInt()));
                client.completeMultipartUpload(new com.obs.services.model.CompleteMultipartUploadRequest(bucket,key,text(q,"upload_id"),parts)); return empty();
            }
            case "mp_abort": client.abortMultipartUpload(new com.obs.services.model.AbortMultipartUploadRequest(bucket,key,text(q,"upload_id"))); return empty();
            default: throw new UnsupportedOperationException(op);
            }
        }
        public void close() { try { client.close(); } catch(IOException error) { throw new UncheckedIOException(error); } }
    }
    /**
     * Measures Tencent COS native requests through its official Java SDK.
     * Bucket-scoped endpoints, complete response reads and pagination preserve the shared workload semantics.
     */
    static final class CosStore implements Store {
        final com.qcloud.cos.COSClient client;
        final String bucket=required("BUCKET");
        CosStore() {
            java.net.URI endpoint=java.net.URI.create(required("ENDPOINT"));
            if(endpoint.getHost()==null || !("http".equals(endpoint.getScheme()) || "https".equals(endpoint.getScheme()))
                || endpoint.getUserInfo()!=null || endpoint.getRawQuery()!=null || endpoint.getFragment()!=null
                || (endpoint.getPath()!=null && !endpoint.getPath().isEmpty() && !"/".equals(endpoint.getPath())))
                throw new IllegalArgumentException("COS endpoint must be an http(s) bucket-domain URL without path or credentials");
            com.qcloud.cos.ClientConfig configuration=new com.qcloud.cos.ClientConfig(new com.qcloud.cos.region.Region(required("REGION")));
            configuration.setMaxErrorRetry(Integer.parseInt(env("RETRIES","0")));
            configuration.setMaxConnectionsCount(Integer.parseInt(env("CONNECTIONS","16")));
            configuration.setHttpProtocol("https".equals(endpoint.getScheme()) ? com.qcloud.cos.http.HttpProtocol.https : com.qcloud.cos.http.HttpProtocol.http);
            configuration.setEndpointBuilder(new com.qcloud.cos.endpoint.UserSpecifiedEndpointBuilder(endpoint.getRawAuthority(),endpoint.getRawAuthority()));
            com.qcloud.cos.auth.COSCredentials credentials=env("SESSION_TOKEN", "").isEmpty()
                ? new com.qcloud.cos.auth.BasicCOSCredentials(required("ACCESS_KEY"), required("SECRET_KEY"))
                : new com.qcloud.cos.auth.BasicSessionCredentials(required("ACCESS_KEY"), required("SECRET_KEY"), required("SESSION_TOKEN"));
            client=new com.qcloud.cos.COSClient(credentials, configuration);
        }
        public Map<String,Object> execute(JsonNode q) throws Exception {
            String key=text(q,"key"), op=text(q,"op");
            switch(op) {
            case "put": client.putObject(bucket,key,new File(text(q,"body_path"))); return empty();
            case "get": {
                com.qcloud.cos.model.GetObjectRequest request=new com.qcloud.cos.model.GetObjectRequest(bucket,key);
                if(q.has("offset")) request.setRange(q.get("offset").asLong(),q.get("offset").asLong()+q.get("length").asLong()-1);
                try(com.qcloud.cos.model.COSObject object=client.getObject(request)) { Files.copy(object.getObjectContent(),Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING); } return empty();
            }
            case "head": return one("size",client.getObjectMetadata(bucket,key).getContentLength());
            case "delete": client.deleteObject(bucket,key); return empty();
            case "list": {
                List<String> keys=new ArrayList<>();
                com.qcloud.cos.model.ListObjectsRequest request=new com.qcloud.cos.model.ListObjectsRequest().withBucketName(bucket).withPrefix(text(q,"prefix"));
                for(;;) {
                    com.qcloud.cos.model.ObjectListing page=client.listObjects(request);
                    for(com.qcloud.cos.model.COSObjectSummary object:page.getObjectSummaries()) keys.add(object.getKey());
                    if(!page.isTruncated()) break;
                    String next=page.getNextMarker();
                    if(next==null || next.isEmpty() || next.equals(request.getMarker())) throw new IOException("invalid pagination marker");
                    request.setMarker(next);
                }
                return one("keys",keys);
            }
            case "mp_create": return one("upload_id",client.initiateMultipartUpload(new com.qcloud.cos.model.InitiateMultipartUploadRequest(bucket,key)).getUploadId());
            case "mp_part": {
                com.qcloud.cos.model.UploadPartRequest request=new com.qcloud.cos.model.UploadPartRequest();
                request.setBucketName(bucket); request.setKey(key); request.setUploadId(text(q,"upload_id")); request.setPartNumber(q.get("part_number").asInt());
                File file=new File(text(q,"body_path")); request.setPartSize(file.length());
                try(InputStream in=new FileInputStream(file)) { request.setInputStream(in); return one("etag",client.uploadPart(request).getETag()); }
            }
            case "mp_complete": {
                List<com.qcloud.cos.model.PartETag> parts=new ArrayList<>();
                for(JsonNode part:q.get("parts")) parts.add(new com.qcloud.cos.model.PartETag(part.get("part_number").asInt(),text(part,"etag")));
                client.completeMultipartUpload(new com.qcloud.cos.model.CompleteMultipartUploadRequest(bucket,key,text(q,"upload_id"),parts)); return empty();
            }
            case "mp_abort": client.abortMultipartUpload(new com.qcloud.cos.model.AbortMultipartUploadRequest(bucket,key,text(q,"upload_id"))); return empty();
            default: throw new UnsupportedOperationException(op);
            }
        }
        public void close() { client.shutdown(); }
    }

}
