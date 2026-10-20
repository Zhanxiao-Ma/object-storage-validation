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
                    start=System.nanoTime();
                    if ("info".equals(text(request,"op"))) {
                        result.put("language", "java"); result.put("backend", provider);
                        result.put("api", "s3".equals(provider)?"s3":"native");
                        result.put("sdk_version", sdkVersion(provider));
                        result.put("protocol_version", 1);
                        result.put("capabilities", Arrays.asList("put", "get", "head", "list", "delete", "mp_create", "mp_part", "mp_complete", "mp_abort", "bucket_create", "list_page", "get_conditional", "put_metadata", "get_metadata", "head_metadata", "copy", "copy_replace"));
                        result.put("endpoint",env("ENDPOINT","")); result.put("region",env("REGION",""));
                        result.put("bucket",env("BUCKET","")); result.put("retries",env("RETRIES","0"));
                        result.put("connections_per_worker",env("CONNECTIONS","16"));
                        result.put("runtime", System.getProperty("java.version"));
                        result.put("cname",Boolean.parseBoolean(env("CNAME","false")));
                        if("obs".equals(provider)) result.put("auth_mode","OBS; negotiation disabled");
                        result.put("measurement", "operation including local payload IO; excludes JSONL serialization");
                    } else result.putAll(store.execute(request));
                    result.put("ok", true);
                } catch (Exception error) {
                    result.put("ok", false);
                    result.put("error_kind", notFound(error) ? "not_found" : "sdk_error");
                    result.put("error", error.getClass().getSimpleName()+": "+error.getMessage());
                    if(error instanceof software.amazon.awssdk.services.s3.model.S3Exception) {
                        software.amazon.awssdk.services.s3.model.S3Exception e=(software.amazon.awssdk.services.s3.model.S3Exception)error;
                        result.put("status_code",e.statusCode()); if(e.awsErrorDetails()!=null) result.put("error_code",e.awsErrorDetails().errorCode());
                    } else if(error instanceof com.aliyun.oss.OSSException) result.put("error_code",((com.aliyun.oss.OSSException)error).getErrorCode());
                    else if(error instanceof com.obs.services.exception.ObsException) { result.put("status_code",((com.obs.services.exception.ObsException)error).getResponseCode()); result.put("error_code",((com.obs.services.exception.ObsException)error).getErrorCode()); }
                    else if(error instanceof com.volcengine.tos.TosServerException) { result.put("status_code",((com.volcengine.tos.TosServerException)error).getStatusCode()); result.put("error_code",((com.volcengine.tos.TosServerException)error).getCode()); }
                    else if(error instanceof com.qcloud.cos.exception.CosServiceException) { result.put("status_code",((com.qcloud.cos.exception.CosServiceException)error).getStatusCode()); result.put("error_code",((com.qcloud.cos.exception.CosServiceException)error).getErrorCode()); }
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
    static Map<String,Object> fields(Object... values) {
        Map<String,Object> result=new LinkedHashMap<>();
        for(int i=0;i<values.length;i+=2) result.put((String)values[i],values[i+1]);
        return result;
    }
    static Map<String,String> userMetadata(JsonNode request) {
        Map<String,String> metadata=new LinkedHashMap<>();
        JsonNode values=request.path("metadata");
        if(!values.isMissingNode() && !values.isObject()) throw new IllegalArgumentException("metadata must be a string map");
        values.fields().forEachRemaining(entry->{
            if(!entry.getValue().isTextual()) throw new IllegalArgumentException("metadata values must be strings");
            metadata.put(entry.getKey(),entry.getValue().asText());
        });
        return metadata;
    }
    static Map<String,Object> withMetadata(Map<String,Object> object,String contentType,Map<?,?> metadata) {
        Map<String,Object> result=new LinkedHashMap<>(object);
        result.put("content_type",contentType);
        result.put("metadata",metadata==null?Collections.emptyMap():metadata);
        return result;
    }
    static Long millis(java.util.Date value) { return value==null?null:value.getTime(); }
    static Long millis(java.time.Instant value) { return value==null?null:value.toEpochMilli(); }
    static Map<String,Object> object(String key,long size,Long modified,String etag) {
        return fields("key",key,"size",size,"last_modified_ms",modified,"etag",etag);
    }
    static Map<String,Object> page(List<Map<String,Object>> objects,List<String> prefixes,boolean truncated,String token,String prefix,String delimiter,int maximum) {
        List<String> keys=new ArrayList<>(); for(Map<String,Object> object:objects) keys.add((String)object.get("key"));
        return fields("keys",keys,"objects",objects,"common_prefixes",prefixes,"truncated",truncated,"next_token",token==null?"":token,"prefix",prefix,"delimiter",delimiter==null?"":delimiter,"max_keys",maximum);
    }


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
            case "copy": case "copy_replace": {
                String sourceBucket=q.path("source_bucket").asText(bucket),sourceKey=text(q,"source_key");
                software.amazon.awssdk.services.s3.model.CopyObjectRequest.Builder request=software.amazon.awssdk.services.s3.model.CopyObjectRequest.builder()
                    .sourceBucket(sourceBucket).sourceKey(sourceKey).destinationBucket(bucket).destinationKey(key);
                if("copy_replace".equals(op)) request.metadataDirective("REPLACE").metadata(userMetadata(q)).contentType(text(q,"content_type"));
                return one("etag",client.copyObject(request.build()).copyObjectResult().eTag());
            }
            case "bucket_create": {
                try { client.headBucket(b->b.bucket(bucket)); }
                catch(software.amazon.awssdk.services.s3.model.S3Exception error) {
                    if(error.statusCode()!=404) throw error;
                    client.createBucket(b->{ b.bucket(bucket); if(!"us-east-1".equals(required("REGION"))) b.createBucketConfiguration(c->c.locationConstraint(required("REGION"))); });
                } return empty();
            }
            case "list_page": {
                software.amazon.awssdk.services.s3.model.ListObjectsV2Request.Builder request=software.amazon.awssdk.services.s3.model.ListObjectsV2Request.builder().bucket(bucket).prefix(text(q,"prefix")).maxKeys(q.path("max_keys").asInt(1000));
                if(q.has("marker")) request.startAfter(text(q,"marker"));
                if(q.has("token")) request.continuationToken(text(q,"token"));
                if(q.has("delimiter")) request.delimiter(text(q,"delimiter"));
                if(q.has("encoding_type")) request.encodingType(text(q,"encoding_type"));
                software.amazon.awssdk.services.s3.model.ListObjectsV2Response result=client.listObjectsV2(request.build());
                List<Map<String,Object>> objects=new ArrayList<>(); List<String> prefixes=new ArrayList<>();
                for(software.amazon.awssdk.services.s3.model.S3Object item:result.contents()) objects.add(object(item.key(),item.size(),millis(item.lastModified()),item.eTag()));
                for(software.amazon.awssdk.services.s3.model.CommonPrefix item:result.commonPrefixes()) prefixes.add(item.prefix());
                return page(objects,prefixes,result.isTruncated(),result.nextContinuationToken(),result.prefix(),result.delimiter(),result.maxKeys());
            }

            case "put_metadata":
                client.putObject(software.amazon.awssdk.services.s3.model.PutObjectRequest.builder().bucket(bucket).key(key)
                    .contentType(text(q,"content_type")).metadata(userMetadata(q)).build(),
                    software.amazon.awssdk.core.sync.RequestBody.fromFile(Paths.get(text(q,"body_path")))); return empty();
            case "put":
                client.putObject(software.amazon.awssdk.services.s3.model.PutObjectRequest.builder().bucket(bucket).key(key).build(),
                    software.amazon.awssdk.core.sync.RequestBody.fromFile(Paths.get(text(q,"body_path")))); return empty();
            case "get": case "get_conditional": case "get_metadata": {
                software.amazon.awssdk.services.s3.model.GetObjectRequest.Builder b=software.amazon.awssdk.services.s3.model.GetObjectRequest.builder().bucket(bucket).key(key);
                if(q.has("if_match")) b.ifMatch(text(q,"if_match"));
                if(q.has("if_none_match")) b.ifNoneMatch(text(q,"if_none_match"));
                if(q.has("if_modified_since_ms")) b.ifModifiedSince(java.time.Instant.ofEpochMilli(q.get("if_modified_since_ms").asLong()));
                if(q.has("if_unmodified_since_ms")) b.ifUnmodifiedSince(java.time.Instant.ofEpochMilli(q.get("if_unmodified_since_ms").asLong()));
                if(q.has("offset")) b.range("bytes="+q.get("offset").asLong()+"-"+(q.get("offset").asLong()+q.get("length").asLong()-1));
                try(software.amazon.awssdk.core.ResponseInputStream<software.amazon.awssdk.services.s3.model.GetObjectResponse> in=client.getObject(b.build())) {
                    Files.copy(in,Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING);
                    software.amazon.awssdk.services.s3.model.GetObjectResponse result=in.response();
                    return withMetadata(object(key,result.contentLength(),millis(result.lastModified()),result.eTag()),result.contentType(),result.metadata());
                }
            }
            case "head": case "head_metadata": { software.amazon.awssdk.services.s3.model.HeadObjectResponse result=client.headObject(b->b.bucket(bucket).key(key)); return withMetadata(object(key,result.contentLength(),millis(result.lastModified()),result.eTag()),result.contentType(),result.metadata()); }
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
            configuration.setSupportCname(Boolean.parseBoolean(env("CNAME","false")));
            configuration.setMaxErrorRetry(Integer.parseInt(env("RETRIES","0")));
            configuration.setMaxConnections(Integer.parseInt(env("CONNECTIONS","16")));
            client=new com.aliyun.oss.OSSClientBuilder().build(required("ENDPOINT"),
                new com.aliyun.oss.common.auth.DefaultCredentialProvider(required("ACCESS_KEY"),required("SECRET_KEY"),env("SESSION_TOKEN",null)),configuration);
        }
        public Map<String,Object> execute(JsonNode q) throws Exception {
            String key=text(q,"key"), op=text(q,"op");
            switch(op) {
            case "copy": case "copy_replace": {
                String sourceBucket=q.path("source_bucket").asText(bucket),sourceKey=text(q,"source_key");
                com.aliyun.oss.model.CopyObjectRequest request=new com.aliyun.oss.model.CopyObjectRequest(sourceBucket,sourceKey,bucket,key);
                if("copy_replace".equals(op)) {
                    com.aliyun.oss.model.ObjectMetadata metadata=new com.aliyun.oss.model.ObjectMetadata();
                    metadata.setContentType(text(q,"content_type")); metadata.setUserMetadata(userMetadata(q));
                    request.setNewObjectMetadata(metadata);
                }
                com.aliyun.oss.model.CopyObjectResult result=client.copyObject(request);
                if(result==null) throw new IllegalStateException("SDK returned no copy result without exposing condition status");
                return one("etag",result.getETag());
            }
            case "bucket_create": if(!client.doesBucketExist(bucket)) client.createBucket(bucket); return empty();
            case "list_page": {
                com.aliyun.oss.model.ListObjectsRequest request=new com.aliyun.oss.model.ListObjectsRequest(bucket);
                request.setPrefix(text(q,"prefix")); request.setMaxKeys(q.path("max_keys").asInt(1000));
                if(q.has("marker") || q.has("token")) request.setMarker(q.has("token")?text(q,"token"):text(q,"marker"));
                if(q.has("delimiter")) request.setDelimiter(text(q,"delimiter"));
                if(q.has("encoding_type")) request.setEncodingType(text(q,"encoding_type"));
                com.aliyun.oss.model.ObjectListing result=client.listObjects(request);
                List<Map<String,Object>> objects=new ArrayList<>();
                for(com.aliyun.oss.model.OSSObjectSummary item:result.getObjectSummaries()) objects.add(object(item.getKey(),item.getSize(),millis(item.getLastModified()),item.getETag()));
                return page(objects,result.getCommonPrefixes(),result.isTruncated(),result.getNextMarker(),result.getPrefix(),result.getDelimiter(),result.getMaxKeys());
            }

            case "put_metadata": {
                com.aliyun.oss.model.ObjectMetadata metadata=new com.aliyun.oss.model.ObjectMetadata();
                metadata.setContentType(text(q,"content_type")); metadata.setUserMetadata(userMetadata(q));
                com.aliyun.oss.model.PutObjectRequest request=new com.aliyun.oss.model.PutObjectRequest(bucket,key,new File(text(q,"body_path")));
                request.setMetadata(metadata); client.putObject(request); return empty();
            }
            case "put": client.putObject(bucket,key,new File(text(q,"body_path"))); return empty();
            case "get": case "get_conditional": case "get_metadata": {
                com.aliyun.oss.model.GetObjectRequest request=new com.aliyun.oss.model.GetObjectRequest(bucket,key);
                if(q.has("if_match")) request.setMatchingETagConstraints(Collections.singletonList(text(q,"if_match")));
                if(q.has("if_none_match")) request.setNonmatchingETagConstraints(Collections.singletonList(text(q,"if_none_match")));
                if(q.has("if_modified_since_ms")) request.setModifiedSinceConstraint(new java.util.Date(q.get("if_modified_since_ms").asLong()));
                if(q.has("if_unmodified_since_ms")) request.setUnmodifiedSinceConstraint(new java.util.Date(q.get("if_unmodified_since_ms").asLong()));
                if(q.has("offset")) request.setRange(q.get("offset").asLong(),q.get("offset").asLong()+q.get("length").asLong()-1);
                try(com.aliyun.oss.model.OSSObject object=client.getObject(request)) {
                    if(object==null) throw new IllegalStateException("SDK returned no object without exposing condition status");
                    Files.copy(object.getObjectContent(),Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING);
                    com.aliyun.oss.model.ObjectMetadata result=object.getObjectMetadata();
                    return withMetadata(object(key,result.getContentLength(),millis(result.getLastModified()),result.getETag()),result.getContentType(),result.getUserMetadata());
                }
            }
            case "head": case "head_metadata": { com.aliyun.oss.model.ObjectMetadata result=client.getObjectMetadata(bucket,key); return withMetadata(object(key,result.getContentLength(),millis(result.getLastModified()),result.getETag()),result.getContentType(),result.getUserMetadata()); }
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
            com.volcengine.tos.TOSClientConfiguration configuration=com.volcengine.tos.TOSClientConfiguration.builder().build()
                .setRegion(required("REGION")).setTransportConfig(transport)
                .setCredentials(new com.volcengine.tos.auth.StaticCredentials(required("ACCESS_KEY"),required("SECRET_KEY")).withSecurityToken(env("SESSION_TOKEN",null)))
                .setCustomDomain(Boolean.parseBoolean(env("CNAME","false")));
            configuration.setEndpoint(required("ENDPOINT"));
            client=new com.volcengine.tos.TOSV2ClientBuilder().build(configuration);
        }
        public Map<String,Object> execute(JsonNode q) throws Exception {
            String key=text(q,"key"),op=text(q,"op");
            switch(op) {
            case "copy": case "copy_replace": {
                String sourceBucket=q.path("source_bucket").asText(bucket),sourceKey=text(q,"source_key");
                com.volcengine.tos.model.object.CopyObjectV2Input request=new com.volcengine.tos.model.object.CopyObjectV2Input()
                    .setBucket(bucket).setKey(key).setSrcBucket(sourceBucket).setSrcKey(sourceKey);
                if("copy_replace".equals(op)) request.setMetadataDirective(com.volcengine.tos.comm.common.MetadataDirectiveType.METADATA_DIRECTIVE_REPLACE)
                    .setOptions(new com.volcengine.tos.model.object.ObjectMetaRequestOptions().setContentType(text(q,"content_type")).setCustomMetadata(userMetadata(q)));
                return one("etag",client.copyObject(request).getEtag());
            }
            case "bucket_create": {
                try { client.headBucket(new com.volcengine.tos.model.bucket.HeadBucketV2Input().setBucket(bucket)); }
                catch(com.volcengine.tos.TosServerException error) { if(error.getStatusCode()!=404) throw error; client.createBucket(bucket); }
                return empty();
            }
            case "list_page": {
                com.volcengine.tos.model.object.ListObjectsType2Input request=new com.volcengine.tos.model.object.ListObjectsType2Input().setBucket(bucket).setPrefix(text(q,"prefix")).setMaxKeys(q.path("max_keys").asInt(1000));
                if(q.has("marker")) request.setStartAfter(text(q,"marker"));
                if(q.has("token")) request.setContinuationToken(text(q,"token"));
                if(q.has("delimiter")) request.setDelimiter(text(q,"delimiter"));
                if(q.has("encoding_type")) request.setEncodingType(text(q,"encoding_type"));
                com.volcengine.tos.model.object.ListObjectsType2Output result=client.listObjectsType2(request);
                List<Map<String,Object>> objects=new ArrayList<>(); List<String> prefixes=new ArrayList<>();
                for(com.volcengine.tos.model.object.ListedObjectV2 item:result.getContents()) objects.add(object(item.getKey(),item.getSize(),millis(item.getLastModified()),item.getEtag()));
                if(result.getCommonPrefixes()!=null) for(com.volcengine.tos.model.object.ListedCommonPrefix item:result.getCommonPrefixes()) prefixes.add(item.getPrefix());
                return page(objects,prefixes,result.isTruncated(),result.getNextContinuationToken(),result.getPrefix(),result.getDelimiter(),result.getMaxKeys());
            }

            case "put_metadata": {
                Path file=Paths.get(text(q,"body_path"));
                com.volcengine.tos.model.object.ObjectMetaRequestOptions metadata=new com.volcengine.tos.model.object.ObjectMetaRequestOptions()
                    .setContentType(text(q,"content_type")).setCustomMetadata(userMetadata(q));
                try(InputStream in=Files.newInputStream(file)) {
                    client.putObject(new com.volcengine.tos.model.object.PutObjectInput().setBucket(bucket).setKey(key)
                        .setContent(in).setContentLength(Files.size(file)).setOptions(metadata));
                } return empty();
            }
            case "put": {
                Path file=Paths.get(text(q,"body_path"));
                try(InputStream in=Files.newInputStream(file)) {
                    client.putObject(new com.volcengine.tos.model.object.PutObjectInput().setBucket(bucket).setKey(key).setContent(in).setContentLength(Files.size(file)));
                } return empty();
            }
            case "get": case "get_conditional": case "get_metadata": {
                com.volcengine.tos.model.object.GetObjectV2Input request=new com.volcengine.tos.model.object.GetObjectV2Input().setBucket(bucket).setKey(key);
                com.volcengine.tos.model.object.ObjectMetaRequestOptions conditions=new com.volcengine.tos.model.object.ObjectMetaRequestOptions();
                if(q.has("if_match")) conditions.setIfMatch(text(q,"if_match"));
                if(q.has("if_none_match")) conditions.setIfNoneMatch(text(q,"if_none_match"));
                if(q.has("if_modified_since_ms")) conditions.setIfModifiedSince(new java.util.Date(q.get("if_modified_since_ms").asLong()));
                if(q.has("if_unmodified_since_ms")) conditions.setIfUnmodifiedSince(new java.util.Date(q.get("if_unmodified_since_ms").asLong()));
                request.setOptions(conditions);
                if(q.has("offset")) request.setRange("bytes="+q.get("offset").asLong()+"-"+(q.get("offset").asLong()+q.get("length").asLong()-1));
                com.volcengine.tos.model.object.GetObjectV2Output result=client.getObject(request);
                try(InputStream in=result.getContent()) { Files.copy(in,Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING); }
                return withMetadata(object(key,result.getContentLength(),millis(result.getLastModifiedInDate()),result.getEtag()),result.getContentType(),result.getCustomMetadata());
            }
            case "head": case "head_metadata": { com.volcengine.tos.model.object.HeadObjectV2Output result=client.headObject(new com.volcengine.tos.model.object.HeadObjectV2Input().setBucket(bucket).setKey(key)); return withMetadata(object(key,result.getContentLength(),millis(result.getLastModifiedInDate()),result.getEtag()),result.getContentType(),result.getCustomMetadata()); }
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
            configuration.setCname(Boolean.parseBoolean(env("CNAME","false")));
            configuration.setAuthType(com.obs.services.model.AuthTypeEnum.OBS);
            configuration.setAuthTypeNegotiation(false);
            String proxy=env("HTTP_PROXY","");
            if(!proxy.isEmpty()) {
                java.net.URI uri=java.net.URI.create(proxy);
                configuration.setHttpProxy(uri.getHost(),uri.getPort()<0?80:uri.getPort(),null,null);
            }
            configuration.setMaxErrorRetry(Integer.parseInt(env("RETRIES","0")));
            configuration.setMaxConnections(Integer.parseInt(env("CONNECTIONS","16")));
            client=new com.obs.services.ObsClient(required("ACCESS_KEY"),required("SECRET_KEY"),env("SESSION_TOKEN",null),configuration);
            if(configuration.getAuthType()!=com.obs.services.model.AuthTypeEnum.OBS) {
                try { client.close(); } catch(IOException error) { throw new UncheckedIOException(error); }
                throw new IllegalArgumentException("OBS native Java SDK requires a DNS endpoint without CNAME or path-style mode; SDK changed authentication to "+configuration.getAuthType());
            }
        }
        public Map<String,Object> execute(JsonNode q) throws Exception {
            String key=text(q,"key"),op=text(q,"op");
            switch(op) {
            case "copy": case "copy_replace": {
                String sourceBucket=q.path("source_bucket").asText(bucket),sourceKey=text(q,"source_key");
                com.obs.services.model.CopyObjectRequest request=new com.obs.services.model.CopyObjectRequest(sourceBucket,sourceKey,bucket,key);
                if("copy_replace".equals(op)) {
                    com.obs.services.model.ObjectMetadata metadata=new com.obs.services.model.ObjectMetadata();
                    metadata.setContentType(text(q,"content_type")); userMetadata(q).forEach(metadata::addUserMetadata);
                    request.setNewObjectMetadata(metadata); request.setReplaceMetadata(true);
                }
                return one("etag",client.copyObject(request).getEtag());
            }
            case "bucket_create": if(!client.headBucket(bucket)) client.createBucket(bucket,required("REGION")); return empty();
            case "list_page": {
                com.obs.services.model.ListObjectsRequest request=new com.obs.services.model.ListObjectsRequest(bucket);
                request.setPrefix(text(q,"prefix")); request.setMaxKeys(q.path("max_keys").asInt(1000));
                if(q.has("marker") || q.has("token")) request.setMarker(q.has("token")?text(q,"token"):text(q,"marker"));
                if(q.has("delimiter")) request.setDelimiter(text(q,"delimiter"));
                if(q.has("encoding_type")) request.setEncodingType(text(q,"encoding_type"));
                com.obs.services.model.ObjectListing result=client.listObjects(request);
                List<Map<String,Object>> objects=new ArrayList<>();
                for(com.obs.services.model.ObsObject item:result.getObjects()) objects.add(object(item.getObjectKey(),item.getMetadata().getContentLength(),millis(item.getMetadata().getLastModified()),item.getMetadata().getEtag()));
                return page(objects,result.getCommonPrefixes(),result.isTruncated(),result.getNextMarker(),result.getPrefix(),result.getDelimiter(),result.getMaxKeys());
            }

            case "put_metadata": {
                com.obs.services.model.ObjectMetadata metadata=new com.obs.services.model.ObjectMetadata();
                metadata.setContentType(text(q,"content_type")); userMetadata(q).forEach(metadata::addUserMetadata);
                com.obs.services.model.PutObjectRequest request=new com.obs.services.model.PutObjectRequest(bucket,key,new File(text(q,"body_path")));
                request.setMetadata(metadata); client.putObject(request); return empty();
            }
            case "put": client.putObject(bucket,key,new File(text(q,"body_path"))); return empty();
            case "get": case "get_conditional": case "get_metadata": {
                com.obs.services.model.GetObjectRequest request=new com.obs.services.model.GetObjectRequest(bucket,key);
                if(q.has("if_match")) request.setIfMatchTag(text(q,"if_match"));
                if(q.has("if_none_match")) request.setIfNoneMatchTag(text(q,"if_none_match"));
                if(q.has("if_modified_since_ms")) request.setIfModifiedSince(new java.util.Date(q.get("if_modified_since_ms").asLong()));
                if(q.has("if_unmodified_since_ms")) request.setIfUnmodifiedSince(new java.util.Date(q.get("if_unmodified_since_ms").asLong()));
                if(q.has("offset")) { request.setRangeStart(q.get("offset").asLong()); request.setRangeEnd(q.get("offset").asLong()+q.get("length").asLong()-1); }
                com.obs.services.model.ObsObject object=client.getObject(request);
                try(InputStream in=object.getObjectContent()) { Files.copy(in,Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING); }
                com.obs.services.model.ObjectMetadata result=object.getMetadata();
                return withMetadata(object(key,result.getContentLength(),millis(result.getLastModified()),result.getEtag()),result.getContentType(),result.getAllMetadata());
            }
            case "head": case "head_metadata": { com.obs.services.model.ObjectMetadata result=client.getObjectMetadata(bucket,key); return withMetadata(object(key,result.getContentLength(),millis(result.getLastModified()),result.getEtag()),result.getContentType(),result.getAllMetadata()); }
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
            // Preserve real conditional service responses; defaults otherwise return null on HTTP 304/412.
            configuration.setThrow304Directly(true);
            configuration.setThrow412Directly(true);
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
            case "copy": case "copy_replace": {
                String sourceBucket=q.path("source_bucket").asText(bucket),sourceKey=text(q,"source_key");
                com.qcloud.cos.model.CopyObjectRequest request=new com.qcloud.cos.model.CopyObjectRequest(sourceBucket,sourceKey,bucket,key);
                if("copy_replace".equals(op)) {
                    com.qcloud.cos.model.ObjectMetadata metadata=new com.qcloud.cos.model.ObjectMetadata();
                    metadata.setContentType(text(q,"content_type")); metadata.setUserMetadata(userMetadata(q));
                    request.setNewObjectMetadata(metadata);
                }
                com.qcloud.cos.model.CopyObjectResult result=client.copyObject(request);
                if(result==null) throw new IllegalStateException("SDK returned no copy result without exposing condition status");
                return one("etag",result.getETag());
            }
            case "bucket_create": if(!client.doesBucketExist(bucket)) client.createBucket(bucket); return empty();
            case "list_page": {
                com.qcloud.cos.model.ListObjectsRequest request=new com.qcloud.cos.model.ListObjectsRequest().withBucketName(bucket).withPrefix(text(q,"prefix")).withMaxKeys(q.path("max_keys").asInt(1000));
                if(q.has("marker") || q.has("token")) request.setMarker(q.has("token")?text(q,"token"):text(q,"marker"));
                if(q.has("delimiter")) request.setDelimiter(text(q,"delimiter"));
                if(q.has("encoding_type")) request.setEncodingType(text(q,"encoding_type"));
                com.qcloud.cos.model.ObjectListing result=client.listObjects(request);
                List<Map<String,Object>> objects=new ArrayList<>();
                for(com.qcloud.cos.model.COSObjectSummary item:result.getObjectSummaries()) objects.add(object(item.getKey(),item.getSize(),millis(item.getLastModified()),item.getETag()));
                return page(objects,result.getCommonPrefixes(),result.isTruncated(),result.getNextMarker(),result.getPrefix(),result.getDelimiter(),result.getMaxKeys());
            }

            case "put_metadata": {
                com.qcloud.cos.model.ObjectMetadata metadata=new com.qcloud.cos.model.ObjectMetadata();
                metadata.setContentType(text(q,"content_type")); metadata.setUserMetadata(userMetadata(q));
                com.qcloud.cos.model.PutObjectRequest request=new com.qcloud.cos.model.PutObjectRequest(bucket,key,new File(text(q,"body_path")));
                request.setMetadata(metadata); client.putObject(request); return empty();
            }
            case "put": client.putObject(bucket,key,new File(text(q,"body_path"))); return empty();
            case "get": case "get_conditional": case "get_metadata": {
                com.qcloud.cos.model.GetObjectRequest request=new com.qcloud.cos.model.GetObjectRequest(bucket,key);
                if(q.has("if_match")) request.setMatchingETagConstraints(Collections.singletonList(text(q,"if_match")));
                if(q.has("if_none_match")) request.setNonmatchingETagConstraints(Collections.singletonList(text(q,"if_none_match")));
                if(q.has("if_modified_since_ms")) request.setModifiedSinceConstraint(new java.util.Date(q.get("if_modified_since_ms").asLong()));
                if(q.has("if_unmodified_since_ms")) request.setUnmodifiedSinceConstraint(new java.util.Date(q.get("if_unmodified_since_ms").asLong()));
                if(q.has("offset")) request.setRange(q.get("offset").asLong(),q.get("offset").asLong()+q.get("length").asLong()-1);
                try(com.qcloud.cos.model.COSObject object=client.getObject(request)) {
                    if(object==null) throw new IllegalStateException("SDK returned no object without exposing condition status");
                    Files.copy(object.getObjectContent(),Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING);
                    com.qcloud.cos.model.ObjectMetadata result=object.getObjectMetadata();
                    return withMetadata(object(key,result.getContentLength(),millis(result.getLastModified()),result.getETag()),result.getContentType(),result.getUserMetadata());
                }
            }
            case "head": case "head_metadata": { com.qcloud.cos.model.ObjectMetadata result=client.getObjectMetadata(bucket,key); return withMetadata(object(key,result.getContentLength(),millis(result.getLastModified()),result.getETag()),result.getContentType(),result.getUserMetadata()); }
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
