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
                        List<String> capabilities=new ArrayList<>(Arrays.asList("put", "get", "head", "list", "delete", "mp_create", "mp_part", "mp_complete", "mp_abort", "bucket_create", "list_page", "get_conditional", "put_metadata", "get_metadata", "head_metadata", "copy", "copy_replace"));
                        if(!"cos".equals(provider)) capabilities.add("get_http");
                        if("s3".equals(provider)) capabilities.addAll(Arrays.asList("tags_put","tags_get","tags_delete","put_http","head_http","put_sse_c","get_sse_c","head_sse_c","put_encryption","get_encryption","head_encryption","mp_create_sse_c","mp_part_sse_c","mp_complete_sse_c","bucket_create_raw","bucket_create_locked","bucket_create_acl","put_acl","bucket_head","bucket_delete","bucket_location","list_v1_page","list_v2_observed","delete_many","delete_many_versions","mp_create_metadata","mp_complete_observed","mp_complete_empty","mp_abort_raw","mp_copy","mp_list","get_part","head_part","versioning_get","versioning_put","versions_list","put_version","get_version","head_version","delete_version"));
                        if("s3".equals(provider)) { capabilities.addAll(S3Identities.capabilities());capabilities.addAll(Arrays.asList("copy_source_version","copy_acl","mp_copy_version","sdk_error_message","concurrent_requests","mp_create_tagging"));capabilities.addAll(S3Checksums.capabilities());if(S3Versioning.supportsDeleteConditions())capabilities.add("delete_many_conditions");if(S3Checksums.has(software.amazon.awssdk.services.s3.model.LoggingEnabled.builder(),"TargetObjectKeyFormat"))capabilities.add("logging_target_key_format"); }
                        if("s3".equals(provider)) for(String api:S3ApiModels.APIS)capabilities.add("api."+api);
                        result.put("capabilities",capabilities);
                        if("s3".equals(provider)) result.put("header_request_timeout_ms",env("HEADER_REQUEST_TIMEOUT_MS","30000"));
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
                    result.putAll(failure(error));
                }
                result.put("elapsed_ns", System.nanoTime()-start);
                protocol.println(JSON.writeValueAsString(result));
            }
        }
    }
    static Map<String,Object> failure(Exception error) {
        Map<String,Object> result=new LinkedHashMap<>();
                    result.put("ok", false);
                    result.put("error_kind", notFound(error) ? "not_found" : "sdk_error");
                    result.put("error", error.getClass().getSimpleName()+": "+error.getMessage());
                    if(error instanceof software.amazon.awssdk.services.s3.model.S3Exception) {
                        software.amazon.awssdk.services.s3.model.S3Exception e=(software.amazon.awssdk.services.s3.model.S3Exception)error;
                        result.put("status_code",e.statusCode()); if(e.awsErrorDetails()!=null && e.awsErrorDetails().sdkHttpResponse()!=null) result.put("http_headers",e.awsErrorDetails().sdkHttpResponse().headers()); if(e.awsErrorDetails()!=null) {result.put("error_code",e.awsErrorDetails().errorCode());result.put("error_message",e.awsErrorDetails().errorMessage());}
                    } else if(error instanceof com.aliyun.oss.OSSException) result.put("error_code",((com.aliyun.oss.OSSException)error).getErrorCode());
                    else if(error instanceof com.obs.services.exception.ObsException) { result.put("status_code",((com.obs.services.exception.ObsException)error).getResponseCode()); result.put("error_code",((com.obs.services.exception.ObsException)error).getErrorCode()); }
                    else if(error instanceof com.volcengine.tos.TosServerException) { result.put("status_code",((com.volcengine.tos.TosServerException)error).getStatusCode()); result.put("error_code",((com.volcengine.tos.TosServerException)error).getCode()); }
                    else if(error instanceof com.qcloud.cos.exception.CosServiceException) { result.put("status_code",((com.qcloud.cos.exception.CosServiceException)error).getStatusCode()); result.put("error_code",((com.qcloud.cos.exception.CosServiceException)error).getErrorCode()); }
        return result;
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
    /** Keep SDK field absence and Owner presence observable; request options never supply response metadata. */
    static Map<String,Object> observedAwsPage(software.amazon.awssdk.services.s3.model.S3Response response,
            List<software.amazon.awssdk.services.s3.model.S3Object> items,
            List<software.amazon.awssdk.services.s3.model.CommonPrefix> prefixes,
            boolean hasItems,boolean hasPrefixes,Map<String,Object> decoded,boolean v2) {
        List<Map<String,Object>> objects=new ArrayList<>();List<String> keys=new ArrayList<>(),groups=new ArrayList<>(),present=new ArrayList<>();
        for(software.amazon.awssdk.services.s3.model.S3Object item:items) {
            if(item.key()==null) throw new IllegalStateException("SDK listing object has no Key");
            Map<String,Object> row=new LinkedHashMap<>();row.put("key",item.key());keys.add(item.key());
            if(item.size()!=null) row.put("size",item.size());
            if(item.lastModified()!=null) row.put("last_modified_ms",millis(item.lastModified()));
            if(item.eTag()!=null) row.put("etag",item.eTag());
            if(item.storageClassAsString()!=null) row.put("storage_class",item.storageClassAsString());
            if(item.owner()!=null) {
                Map<String,Object> owner=new LinkedHashMap<>();
                if(item.owner().id()!=null) owner.put("id",item.owner().id());
                if(item.owner().displayName()!=null) owner.put("display_name",item.owner().displayName());
                row.put("owner",owner);
            }
            objects.add(row);
        }
        for(software.amazon.awssdk.services.s3.model.CommonPrefix prefix:prefixes) groups.add(prefix.prefix());
        decoded.forEach((name,value)->{if(value!=null)present.add(name);});
        if(hasItems)present.add("Contents");if(hasPrefixes)present.add("CommonPrefixes");
        Map<String,Object> observed=fields("keys",keys,"objects",objects,"common_prefixes",groups,"fields",present,
            "prefix",decoded.get("Prefix"),"delimiter",decoded.get("Delimiter"),"max_keys",decoded.get("MaxKeys"),"truncated",decoded.get("IsTruncated"));
        if(v2) observed.putAll(fields("key_count",decoded.get("KeyCount"),"token",decoded.get("ContinuationToken"),"next_token",decoded.get("NextContinuationToken"),"start_after",decoded.get("StartAfter")));
        else observed.putAll(fields("marker",decoded.get("Marker"),"next_marker",decoded.get("NextMarker")));
        return withHttp(observed,response.sdkHttpResponse().statusCode(),response.sdkHttpResponse().headers());
    }
    static Map<String,Object> withMetadata(Map<String,Object> object,String contentType,Map<?,?> metadata) {
        Map<String,Object> result=new LinkedHashMap<>(object);
        result.put("content_type",contentType);
        result.put("metadata",metadata==null?Collections.emptyMap():metadata);
        return result;
    }
    static Map<String,Object> withHttp(Map<String,Object> object,int status,Map<?,?> headers) {
        if(status<=0) throw new IllegalStateException("SDK did not expose an HTTP response status");
        Map<String,String> observed=new LinkedHashMap<>();
        if(headers!=null) headers.forEach((key,value)->{
            String text=value instanceof List?String.join(",",((List<?>)value).stream().map(String::valueOf).toArray(String[]::new)):String.valueOf(value);
            observed.put(String.valueOf(key).toLowerCase(Locale.ROOT),text);
        });
        Map<String,Object> result=new LinkedHashMap<>(object);
        result.put("status_code",status); result.put("http_headers",observed);
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
        final S3Identities identities=new S3Identities();
        AwsStore() { client=identities.get("main"); }
        public Map<String,Object> execute(JsonNode q) throws Exception {
            if("concurrent_requests".equals(text(q,"op")))return S3Concurrency.execute(this,q);
            if(q.has("wire_headers")) {
                try(WireHeaders.Scope ignored=new WireHeaders.Scope(q.get("wire_headers"))) {
                    return executeSelected(q,identities.select(q));
                }
            }
            return executeSelected(q,(q.has("identity")||q.has("checksum_when_required"))?identities.select(q):this.client);
        }
        private Map<String,Object> executeSelected(JsonNode q,software.amazon.awssdk.services.s3.S3Client client) throws Exception {
            String key=text(q,"key"), op=text(q,"op");
            if("put_encryption".equals(op)) op="put_sse_c";
            else if("get_encryption".equals(op)) op="get_sse_c";
            else if("head_encryption".equals(op)) op="head_sse_c";
            String bucket=q.has("bucket")?text(q,"bucket"):this.bucket;
            if(S3Versioning.handles(op)) return S3Versioning.execute(client,op,q,bucket,key);
            if(S3ApiModels.handles(op)) return S3ApiModels.execute(client,op,q);
            switch(op) {
            case "delete_many": case "delete_many_versions": {
                List<software.amazon.awssdk.services.s3.model.ObjectIdentifier> identifiers=new ArrayList<>();
                if(!q.has("objects") || !q.get("objects").isArray())throw new IllegalArgumentException("objects must be an array");
                Set<String> allowed=Set.of("key","version_id","etag","last_modified_ms","size");
                for(JsonNode item:q.get("objects")) {
                    if(!item.isObject())throw new IllegalArgumentException("delete identifier must be an object");
                    item.fieldNames().forEachRemaining(name->{if(!allowed.contains(name))throw new IllegalArgumentException("unknown delete identifier member: "+name);});
                    if(!item.has("key") || !item.get("key").isTextual())throw new IllegalArgumentException("key must be textual");
                    if(item.has("version_id") && !item.get("version_id").isTextual())throw new IllegalArgumentException("version_id must be textual");
                    software.amazon.awssdk.services.s3.model.ObjectIdentifier.Builder id=software.amazon.awssdk.services.s3.model.ObjectIdentifier.builder().key(text(item,"key"));
                    if(item.has("version_id")) id.versionId(text(item,"version_id"));
                    S3Versioning.applyDeleteConditions(id,item);
                    identifiers.add(id.build());
                }
                software.amazon.awssdk.services.s3.model.Delete.Builder deletion=software.amazon.awssdk.services.s3.model.Delete.builder().objects(identifiers);
                if(q.has("quiet")) deletion.quiet(q.get("quiet").asBoolean());
                software.amazon.awssdk.services.s3.model.DeleteObjectsResponse result=client.deleteObjects(b->b.bucket(bucket).delete(deletion.build()));
                List<Map<String,Object>> deleted=new ArrayList<>(),errors=new ArrayList<>();
                for(software.amazon.awssdk.services.s3.model.DeletedObject item:result.deleted()) {
                    Map<String,Object> row=new LinkedHashMap<>();
                    if(item.key()!=null)row.put("key",item.key());if(item.versionId()!=null)row.put("version_id",item.versionId());
                    if(item.deleteMarker()!=null)row.put("delete_marker",item.deleteMarker());if(item.deleteMarkerVersionId()!=null)row.put("delete_marker_version_id",item.deleteMarkerVersionId());deleted.add(row);
                }
                for(software.amazon.awssdk.services.s3.model.S3Error item:result.errors()) {
                    Map<String,Object> row=new LinkedHashMap<>();
                    if(item.key()!=null)row.put("key",item.key());if(item.versionId()!=null)row.put("version_id",item.versionId());
                    if(item.code()!=null)row.put("code",item.code());if(item.message()!=null)row.put("message",item.message());errors.add(row);
                }
                return withHttp(fields("deleted",deleted,"errors",errors,"errors_present",result.hasErrors()),result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
            }
            case "list_v1_page": {
                software.amazon.awssdk.services.s3.model.ListObjectsRequest.Builder request=software.amazon.awssdk.services.s3.model.ListObjectsRequest.builder().bucket(bucket);
                if(q.has("prefix"))request.prefix(text(q,"prefix"));if(q.has("delimiter"))request.delimiter(text(q,"delimiter"));
                if(q.has("marker"))request.marker(text(q,"marker"));if(q.has("max_keys"))request.maxKeys(q.get("max_keys").asInt());
                if(q.has("encoding_type"))request.encodingType(text(q,"encoding_type"));
                software.amazon.awssdk.services.s3.model.ListObjectsResponse result=client.listObjects(request.build());
                return observedAwsPage(result,result.contents(),result.commonPrefixes(),result.hasContents(),result.hasCommonPrefixes(),
                    fields("Name",result.name(),"Prefix",result.prefix(),"Delimiter",result.delimiter(),"Marker",result.marker(),"NextMarker",result.nextMarker(),"MaxKeys",result.maxKeys(),"IsTruncated",result.isTruncated(),"EncodingType",result.encodingTypeAsString()),false);
            }
            case "list_v2_observed": {
                software.amazon.awssdk.services.s3.model.ListObjectsV2Request.Builder request=software.amazon.awssdk.services.s3.model.ListObjectsV2Request.builder().bucket(bucket);
                if(q.has("prefix"))request.prefix(text(q,"prefix"));if(q.has("delimiter"))request.delimiter(text(q,"delimiter"));
                if(q.has("start_after"))request.startAfter(text(q,"start_after"));if(q.has("token"))request.continuationToken(text(q,"token"));
                if(q.has("max_keys"))request.maxKeys(q.get("max_keys").asInt());if(q.has("fetch_owner"))request.fetchOwner(q.get("fetch_owner").asBoolean());
                if(q.has("encoding_type"))request.encodingType(text(q,"encoding_type"));
                software.amazon.awssdk.services.s3.model.ListObjectsV2Response result=client.listObjectsV2(request.build());
                return observedAwsPage(result,result.contents(),result.commonPrefixes(),result.hasContents(),result.hasCommonPrefixes(),
                    fields("Name",result.name(),"Prefix",result.prefix(),"Delimiter",result.delimiter(),"ContinuationToken",result.continuationToken(),"NextContinuationToken",result.nextContinuationToken(),"StartAfter",result.startAfter(),"KeyCount",result.keyCount(),"MaxKeys",result.maxKeys(),"IsTruncated",result.isTruncated(),"EncodingType",result.encodingTypeAsString()),true);
            }
            case "bucket_create_raw": case "bucket_create_locked": case "bucket_create_acl": {
                software.amazon.awssdk.services.s3.model.CreateBucketRequest.Builder request=software.amazon.awssdk.services.s3.model.CreateBucketRequest.builder().bucket(bucket);
                if("bucket_create_locked".equals(op)) request.objectLockEnabledForBucket(true);
                if("bucket_create_acl".equals(op)) request.acl(text(q,"acl"));
                if(q.has("location")) request.createBucketConfiguration(c->c.locationConstraint(text(q,"location")));
                software.amazon.awssdk.services.s3.model.CreateBucketResponse result=client.createBucket(request.build());
                return withHttp(empty(),result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
            }
            case "bucket_head": {
                software.amazon.awssdk.services.s3.model.HeadBucketResponse result=client.headBucket(b->b.bucket(bucket));
                return withHttp(empty(),result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
            }
            case "bucket_delete": {
                software.amazon.awssdk.services.s3.model.DeleteBucketResponse result=client.deleteBucket(b->b.bucket(bucket));
                return withHttp(empty(),result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
            }
            case "bucket_location": {
                software.amazon.awssdk.services.s3.model.GetBucketLocationResponse result=client.getBucketLocation(b->b.bucket(bucket));
                return withHttp(one("location",result.locationConstraintAsString()),result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
            }
            case "put_http": case "put_sse_c": case "put_acl": {
                software.amazon.awssdk.services.s3.model.PutObjectRequest.Builder request=software.amazon.awssdk.services.s3.model.PutObjectRequest.builder().bucket(bucket).key(key);
                if("put_sse_c".equals(op)) request.overrideConfiguration(EncryptionHeaders.fromRequest(q));
                if("put_acl".equals(op)) request.acl(text(q,"acl"));
                if(q.has("content_length")) request.contentLength(q.get("content_length").asLong());
                S3Checksums.apply((software.amazon.awssdk.core.SdkPojo)request,q);
                if(q.has("cache_control")) request.cacheControl(text(q,"cache_control"));
                if(q.has("expires_ms")) request.expires(java.time.Instant.ofEpochMilli(q.get("expires_ms").asLong()));
                software.amazon.awssdk.services.s3.model.PutObjectResponse result=client.putObject(request.build(),software.amazon.awssdk.core.sync.RequestBody.fromFile(Paths.get(text(q,"body_path"))));
                return S3Checksums.observe(withHttp(one("etag",result.eTag()),result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers()),result);
            }
            case "head_http": case "head_sse_c": {
                software.amazon.awssdk.services.s3.model.HeadObjectRequest.Builder request=software.amazon.awssdk.services.s3.model.HeadObjectRequest.builder().bucket(bucket).key(key);
                if("head_sse_c".equals(op)) request.overrideConfiguration(EncryptionHeaders.fromRequest(q));
                if(q.has("part_number")) request.partNumber(q.get("part_number").asInt());
                S3Checksums.apply((software.amazon.awssdk.core.SdkPojo)request,q);
                software.amazon.awssdk.services.s3.model.HeadObjectResponse result=client.headObject(request.build());
                Map<String,Object> observed=object(key,result.contentLength(),millis(result.lastModified()),result.eTag());
                if(q.has("part_number")) observed.put("parts_count",result.partsCount());
                return S3Checksums.observe(withHttp(observed,result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers()),result);
            }
            case "tags_put": {
                List<software.amazon.awssdk.services.s3.model.Tag> tags=new ArrayList<>();
                for(JsonNode tag:q.get("tags")) tags.add(software.amazon.awssdk.services.s3.model.Tag.builder().key(text(tag,"key")).value(text(tag,"value")).build());
                software.amazon.awssdk.services.s3.model.PutObjectTaggingResponse result=client.putObjectTagging(b->b.bucket(bucket).key(key).tagging(t->t.tagSet(tags)));
                return withHttp(empty(),result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
            }
            case "tags_get": {
                software.amazon.awssdk.services.s3.model.GetObjectTaggingResponse result=client.getObjectTagging(b->b.bucket(bucket).key(key));
                List<Map<String,Object>> tags=new ArrayList<>();
                for(software.amazon.awssdk.services.s3.model.Tag tag:result.tagSet()) {Map<String,Object> pair=new LinkedHashMap<>();pair.put("key",tag.key());pair.put("value",tag.value());tags.add(pair);}
                return withHttp(one("tags",tags),result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
            }
            case "tags_delete": {
                software.amazon.awssdk.services.s3.model.DeleteObjectTaggingResponse result=client.deleteObjectTagging(b->b.bucket(bucket).key(key));
                return withHttp(empty(),result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
            }
            case "copy": case "copy_replace": {
                String sourceBucket=q.path("source_bucket").asText(bucket),sourceKey=text(q,"source_key");
                software.amazon.awssdk.services.s3.model.CopyObjectRequest.Builder request=software.amazon.awssdk.services.s3.model.CopyObjectRequest.builder()
                    .sourceBucket(sourceBucket).sourceKey(sourceKey).destinationBucket(bucket).destinationKey(key);
                if(q.has("source_version_id"))request.sourceVersionId(text(q,"source_version_id"));
                if(q.has("acl"))request.acl(text(q,"acl"));
                if("copy_replace".equals(op)) {
                    request.metadataDirective("REPLACE").metadata(userMetadata(q));
                    if(q.has("content_type")) request.contentType(text(q,"content_type"));
                }
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
            case "get": case "get_conditional": case "get_metadata": case "get_http": case "get_sse_c": case "get_part": {
                software.amazon.awssdk.services.s3.model.GetObjectRequest.Builder b=software.amazon.awssdk.services.s3.model.GetObjectRequest.builder().bucket(bucket).key(key);
                if("get_sse_c".equals(op)) b.overrideConfiguration(EncryptionHeaders.fromRequest(q));
                if("get_part".equals(op)) S3Checksums.apply((software.amazon.awssdk.core.SdkPojo)b,q);
                if(q.has("if_match")) b.ifMatch(text(q,"if_match"));
                if(q.has("if_none_match")) b.ifNoneMatch(text(q,"if_none_match"));
                if(q.has("if_modified_since_ms")) b.ifModifiedSince(java.time.Instant.ofEpochMilli(q.get("if_modified_since_ms").asLong()));
                if(q.has("if_unmodified_since_ms")) b.ifUnmodifiedSince(java.time.Instant.ofEpochMilli(q.get("if_unmodified_since_ms").asLong()));
                if(q.has("part_number")) b.partNumber(q.get("part_number").asInt());
                if(q.has("range_header")) b.range(text(q,"range_header"));
                if(q.has("offset")) b.range("bytes="+q.get("offset").asLong()+"-"+(q.get("offset").asLong()+q.get("length").asLong()-1));
                try(software.amazon.awssdk.core.ResponseInputStream<software.amazon.awssdk.services.s3.model.GetObjectResponse> in=client.getObject(b.build())) {
                    Files.copy(in,Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING);
                    software.amazon.awssdk.services.s3.model.GetObjectResponse result=in.response();
                    Map<String,Object> observed=withMetadata(object(key,result.contentLength(),millis(result.lastModified()),result.eTag()),result.contentType(),result.metadata());
                    if("get_part".equals(op)||("get_sse_c".equals(op)&&q.has("part_number"))) observed.put("parts_count",result.partsCount());
                    if("get_part".equals(op)) S3Checksums.observe(observed,result);
                    return ("get_http".equals(op)||"get_sse_c".equals(op))?withHttp(observed,result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers()):observed;
                }
            }
            case "head": case "head_metadata": case "head_part": {
                software.amazon.awssdk.services.s3.model.HeadObjectRequest.Builder b=software.amazon.awssdk.services.s3.model.HeadObjectRequest.builder().bucket(bucket).key(key);
                if(q.has("part_number")) b.partNumber(q.get("part_number").asInt());
                software.amazon.awssdk.services.s3.model.HeadObjectResponse result=client.headObject(b.build());
                Map<String,Object> observed=withMetadata(object(key,result.contentLength(),millis(result.lastModified()),result.eTag()),result.contentType(),result.metadata());
                if("head_part".equals(op)) observed.put("parts_count",result.partsCount());return observed;
            }
            case "delete": client.deleteObject(b->b.bucket(bucket).key(key)); return empty();
            case "list": {
                List<String> keys=new ArrayList<>();
                for(software.amazon.awssdk.services.s3.model.ListObjectsV2Response page:client.listObjectsV2Paginator(b->b.bucket(bucket).prefix(text(q,"prefix"))))
                    for(software.amazon.awssdk.services.s3.model.S3Object object:page.contents()) keys.add(object.key());
                return one("keys",keys);
            }
            case "mp_copy": {
                software.amazon.awssdk.services.s3.model.UploadPartCopyRequest.Builder b=software.amazon.awssdk.services.s3.model.UploadPartCopyRequest.builder()
                    .sourceBucket(q.path("source_bucket").asText(bucket)).sourceKey(text(q,"source_key")).destinationBucket(bucket).destinationKey(key)
                    .uploadId(text(q,"upload_id")).partNumber(q.get("part_number").asInt());
                if(q.has("range_header")) b.copySourceRange(text(q,"range_header"));
                if(q.has("version_id")) b.sourceVersionId(text(q,"version_id"));
                software.amazon.awssdk.services.s3.model.UploadPartCopyResponse result=client.uploadPartCopy(b.build());
                return withHttp(one("etag",result.copyPartResult().eTag()),result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
            }
            case "mp_list": {
                software.amazon.awssdk.services.s3.model.ListMultipartUploadsRequest.Builder b=software.amazon.awssdk.services.s3.model.ListMultipartUploadsRequest.builder().bucket(bucket);
                if(q.has("prefix")) b.prefix(text(q,"prefix"));
                if(q.has("delimiter")) b.delimiter(text(q,"delimiter"));
                if(q.has("key_marker")) b.keyMarker(text(q,"key_marker"));
                if(q.has("upload_id_marker")) b.uploadIdMarker(text(q,"upload_id_marker"));
                if(q.has("max_uploads")) b.maxUploads(q.get("max_uploads").asInt());
                software.amazon.awssdk.services.s3.model.ListMultipartUploadsResponse result=client.listMultipartUploads(b.build());
                List<Map<String,Object>> uploads=new ArrayList<>();
                for(software.amazon.awssdk.services.s3.model.MultipartUpload item:result.uploads()) {
                    Map<String,Object> row=new LinkedHashMap<>();row.put("key",item.key());row.put("upload_id",item.uploadId());row.put("initiated_ms",millis(item.initiated()));row.put("storage_class",item.storageClassAsString());
                    if(item.owner()!=null) {Map<String,Object> owner=new LinkedHashMap<>();owner.put("id",item.owner().id());owner.put("display_name",item.owner().displayName());row.put("owner",owner);}
                    if(item.initiator()!=null) {Map<String,Object> owner=new LinkedHashMap<>();owner.put("id",item.initiator().id());owner.put("display_name",item.initiator().displayName());row.put("initiator",owner);}
                    uploads.add(row);
                }
                Map<String,Object> observed=new LinkedHashMap<>();observed.put("uploads",uploads);observed.put("truncated",result.isTruncated());observed.put("next_key_marker",result.nextKeyMarker());observed.put("next_upload_id_marker",result.nextUploadIdMarker());
                observed.put("key_marker",result.keyMarker());observed.put("upload_id_marker",result.uploadIdMarker());observed.put("max_uploads",result.maxUploads());observed.put("prefix",result.prefix());observed.put("delimiter",result.delimiter());
                List<String> prefixes=new ArrayList<>();for(software.amazon.awssdk.services.s3.model.CommonPrefix item:result.commonPrefixes()) prefixes.add(item.prefix());observed.put("common_prefixes",prefixes);
                return withHttp(observed,result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
            }
            case "mp_create": case "mp_create_metadata": case "mp_create_sse_c": {
                software.amazon.awssdk.services.s3.model.CreateMultipartUploadRequest.Builder b=software.amazon.awssdk.services.s3.model.CreateMultipartUploadRequest.builder().bucket(bucket).key(key);
                if("mp_create_sse_c".equals(op)) b.overrideConfiguration(EncryptionHeaders.fromRequest(q));
                if(q.has("content_type")) b.contentType(text(q,"content_type"));
                if(q.has("metadata")) b.metadata(JSON.convertValue(q.get("metadata"),new com.fasterxml.jackson.core.type.TypeReference<Map<String,String>>(){}));
                if(q.has("tagging")) {
                    if(!q.get("tagging").isTextual())throw new IllegalArgumentException("tagging must be textual");
                    b.tagging(q.get("tagging").textValue());
                }
                S3Checksums.apply((software.amazon.awssdk.core.SdkPojo)b,q);
                software.amazon.awssdk.services.s3.model.CreateMultipartUploadResponse result=client.createMultipartUpload(b.build());
                return S3Checksums.observe(withHttp(one("upload_id",result.uploadId()),result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers()),result);
            }
            case "mp_part": case "mp_part_sse_c": {
                software.amazon.awssdk.services.s3.model.UploadPartRequest.Builder b=software.amazon.awssdk.services.s3.model.UploadPartRequest.builder().bucket(bucket).key(key).uploadId(text(q,"upload_id")).partNumber(q.get("part_number").asInt());
                if("mp_part_sse_c".equals(op)) b.overrideConfiguration(EncryptionHeaders.fromRequest(q));
                S3Checksums.apply((software.amazon.awssdk.core.SdkPojo)b,q);
                software.amazon.awssdk.services.s3.model.UploadPartResponse result=client.uploadPart(b.build(),software.amazon.awssdk.core.sync.RequestBody.fromFile(Paths.get(text(q,"body_path"))));
                return S3Checksums.observe(withHttp(one("etag",result.eTag()),result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers()),result);
            }
            case "mp_complete": case "mp_complete_observed": case "mp_complete_empty": case "mp_complete_sse_c": {
                List<software.amazon.awssdk.services.s3.model.CompletedPart> parts=new ArrayList<>();
                for(JsonNode part:q.path("parts")) {
                    software.amazon.awssdk.services.s3.model.CompletedPart.Builder item=software.amazon.awssdk.services.s3.model.CompletedPart.builder().partNumber(part.get("part_number").asInt()).eTag(text(part,"etag"));
                    com.fasterxml.jackson.databind.node.ObjectNode checksum=JSON.createObjectNode(),options=JSON.createObjectNode();
                    part.fields().forEachRemaining(entry->{if(entry.getKey().startsWith("Checksum"))checksum.set(entry.getKey(),entry.getValue());});
                    options.set("checksum",checksum);S3Checksums.apply((software.amazon.awssdk.core.SdkPojo)item,options);parts.add(item.build());
                }
                software.amazon.awssdk.services.s3.model.CompleteMultipartUploadRequest.Builder b=software.amazon.awssdk.services.s3.model.CompleteMultipartUploadRequest.builder().bucket(bucket).key(key).uploadId(text(q,"upload_id"));
                if("mp_complete_sse_c".equals(op)) b.overrideConfiguration(EncryptionHeaders.fromRequest(q));
                if(!"mp_complete_empty".equals(op)) b.multipartUpload(m->m.parts(parts));
                S3Checksums.apply((software.amazon.awssdk.core.SdkPojo)b,q);
                software.amazon.awssdk.services.s3.model.CompleteMultipartUploadResponse result=client.completeMultipartUpload(b.build());
                Map<String,Object> observed=fields("etag",result.eTag());
                if(result.versionId()!=null) observed.put("version_id",result.versionId());
                return S3Checksums.observe(withHttp(observed,result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers()),result);
            }
            case "mp_abort": case "mp_abort_raw": client.abortMultipartUpload(b->b.bucket(bucket).key(key).uploadId(text(q,"upload_id"))); return empty();
            default: throw new UnsupportedOperationException(op);
            }
        }
        public void close() { identities.close(); }
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
            case "get": case "get_conditional": case "get_metadata": case "get_http": {
                com.aliyun.oss.model.GetObjectRequest request=new com.aliyun.oss.model.GetObjectRequest(bucket,key);
                if(q.has("range_header")) request.addHeader("Range",text(q,"range_header"));
                if(q.has("if_match")) request.setMatchingETagConstraints(Collections.singletonList(text(q,"if_match")));
                if(q.has("if_none_match")) request.setNonmatchingETagConstraints(Collections.singletonList(text(q,"if_none_match")));
                if(q.has("if_modified_since_ms")) request.setModifiedSinceConstraint(new java.util.Date(q.get("if_modified_since_ms").asLong()));
                if(q.has("if_unmodified_since_ms")) request.setUnmodifiedSinceConstraint(new java.util.Date(q.get("if_unmodified_since_ms").asLong()));
                if(q.has("offset")) request.setRange(q.get("offset").asLong(),q.get("offset").asLong()+q.get("length").asLong()-1);
                try(com.aliyun.oss.model.OSSObject object=client.getObject(request)) {
                    if(object==null) throw new IllegalStateException("SDK returned no object without exposing condition status");
                    Files.copy(object.getObjectContent(),Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING);
                    com.aliyun.oss.model.ObjectMetadata result=object.getObjectMetadata();
                    Map<String,Object> observed=withMetadata(object(key,result.getContentLength(),millis(result.getLastModified()),result.getETag()),result.getContentType(),result.getUserMetadata());
                    return "get_http".equals(op)?withHttp(observed,object.getResponse().getStatusCode(),object.getResponse().getHeaders()):observed;
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
            case "get": case "get_conditional": case "get_metadata": case "get_http": {
                com.volcengine.tos.model.object.GetObjectV2Input request=new com.volcengine.tos.model.object.GetObjectV2Input().setBucket(bucket).setKey(key);
                if(q.has("range_header")) request.setRange(text(q,"range_header"));
                com.volcengine.tos.model.object.ObjectMetaRequestOptions conditions=new com.volcengine.tos.model.object.ObjectMetaRequestOptions();
                if(q.has("if_match")) conditions.setIfMatch(text(q,"if_match"));
                if(q.has("if_none_match")) conditions.setIfNoneMatch(text(q,"if_none_match"));
                if(q.has("if_modified_since_ms")) conditions.setIfModifiedSince(new java.util.Date(q.get("if_modified_since_ms").asLong()));
                if(q.has("if_unmodified_since_ms")) conditions.setIfUnmodifiedSince(new java.util.Date(q.get("if_unmodified_since_ms").asLong()));
                request.setOptions(conditions);
                if(q.has("offset")) request.setRange("bytes="+q.get("offset").asLong()+"-"+(q.get("offset").asLong()+q.get("length").asLong()-1));
                com.volcengine.tos.model.object.GetObjectV2Output result=client.getObject(request);
                try(InputStream in=result.getContent()) { Files.copy(in,Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING); }
                Map<String,Object> observed=withMetadata(object(key,result.getContentLength(),millis(result.getLastModifiedInDate()),result.getEtag()),result.getContentType(),result.getCustomMetadata());
                return "get_http".equals(op)?withHttp(observed,result.getRequestInfo().getStatusCode(),result.getRequestInfo().getHeader()):observed;
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
            case "get": case "get_conditional": case "get_metadata": case "get_http": {
                com.obs.services.model.GetObjectRequest request=new com.obs.services.model.GetObjectRequest(bucket,key);
                if(q.has("range_header")) request.addUserHeaders("Range",text(q,"range_header"));
                if(q.has("if_match")) request.setIfMatchTag(text(q,"if_match"));
                if(q.has("if_none_match")) request.setIfNoneMatchTag(text(q,"if_none_match"));
                if(q.has("if_modified_since_ms")) request.setIfModifiedSince(new java.util.Date(q.get("if_modified_since_ms").asLong()));
                if(q.has("if_unmodified_since_ms")) request.setIfUnmodifiedSince(new java.util.Date(q.get("if_unmodified_since_ms").asLong()));
                if(q.has("offset")) { request.setRangeStart(q.get("offset").asLong()); request.setRangeEnd(q.get("offset").asLong()+q.get("length").asLong()-1); }
                com.obs.services.model.ObsObject object=client.getObject(request);
                try(InputStream in=object.getObjectContent()) { Files.copy(in,Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING); }
                com.obs.services.model.ObjectMetadata result=object.getMetadata();
                Map<String,Object> observed=withMetadata(object(key,result.getContentLength(),millis(result.getLastModified()),result.getEtag()),result.getContentType(),result.getAllMetadata());
                return "get_http".equals(op)?withHttp(observed,result.getStatusCode(),result.getOriginalHeaders()):observed;
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
                if(q.has("range_header")) request.putCustomRequestHeader("Range",text(q,"range_header"));
                if(q.has("if_match")) request.setMatchingETagConstraints(Collections.singletonList(text(q,"if_match")));
                if(q.has("if_none_match")) request.setNonmatchingETagConstraints(Collections.singletonList(text(q,"if_none_match")));
                if(q.has("if_modified_since_ms")) request.setModifiedSinceConstraint(new java.util.Date(q.get("if_modified_since_ms").asLong()));
                if(q.has("if_unmodified_since_ms")) request.setUnmodifiedSinceConstraint(new java.util.Date(q.get("if_unmodified_since_ms").asLong()));
                if(q.has("offset")) request.setRange(q.get("offset").asLong(),q.get("offset").asLong()+q.get("length").asLong()-1);
                try(com.qcloud.cos.model.COSObject object=client.getObject(request)) {
                    if(object==null) throw new IllegalStateException("SDK returned no object without exposing condition status");
                    Files.copy(object.getObjectContent(),Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING);
                    com.qcloud.cos.model.ObjectMetadata result=object.getObjectMetadata();
                    Map<String,Object> observed=withMetadata(object(key,result.getContentLength(),millis(result.getLastModified()),result.getETag()),result.getContentType(),result.getUserMetadata());
                    return observed;
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
