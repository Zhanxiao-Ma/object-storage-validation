package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.util.*;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.*;
import static io.github.storagevalidation.WorkerMain.*;

/**
 * Exposes actual versioning operations that ordinary current-object IO cannot verify.
 * Delegates to the AWS SDK, preserves returned IDs, markers and field absence, and
 * writes downloaded bodies directly so language-neutral cases can inspect real bytes.
 */
final class S3Versioning {
    private static final Set<String> OPERATIONS=Set.of("versioning_get","versioning_put","versions_list","put_version","get_version","head_version","delete_version");

    static boolean handles(String operation) { return OPERATIONS.contains(operation); }

    static boolean supportsDeleteConditions() {
        Set<String> fields=new HashSet<>();
        for(software.amazon.awssdk.core.SdkField<?> field:ObjectIdentifier.builder().sdkFields())fields.add(field.memberName());
        return fields.containsAll(Arrays.asList("ETag","LastModifiedTime","Size"));
    }

    static void applyDeleteConditions(ObjectIdentifier.Builder builder,JsonNode item) {
        com.fasterxml.jackson.databind.node.ObjectNode fields=JSON.createObjectNode();
        if(item.has("etag")) {
            if(!item.get("etag").isTextual())throw new IllegalArgumentException("etag must be textual");
            fields.set("ETag",item.get("etag"));
        }
        for(String name:Arrays.asList("last_modified_ms","size"))if(item.has(name)) {
            JsonNode value=item.get(name);
            if(!value.isIntegralNumber() || !value.canConvertToLong())throw new IllegalArgumentException(name+" must be a signed 64-bit integer");
            fields.set("size".equals(name)?"Size":"LastModifiedTime",value);
        }
        if(fields.size()>0)SdkModelJson.read(builder,fields);
    }

    static Map<String,Object> execute(S3Client client,String op,JsonNode q,String bucket,String key) throws Exception {
        switch(op) {
        case "versioning_get": {
            GetBucketVersioningResponse result=client.getBucketVersioning(b->b.bucket(bucket));
            Map<String,Object> observed=new LinkedHashMap<>();
            if(result.statusAsString()!=null) observed.put("versioning_status",result.statusAsString());
            if(result.mfaDeleteAsString()!=null) observed.put("mfa_delete",result.mfaDeleteAsString());
            return withHttp(observed,result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
        }
        case "versioning_put": {
            VersioningConfiguration.Builder configuration=VersioningConfiguration.builder().status(text(q,"status"));
            if(q.has("mfa_delete")) configuration.mfaDelete(text(q,"mfa_delete"));
            PutBucketVersioningResponse result=client.putBucketVersioning(b->b.bucket(bucket).versioningConfiguration(configuration.build()));
            return withHttp(new LinkedHashMap<>(),result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
        }
        case "put_version": {
            PutObjectResponse result=client.putObject(b->b.bucket(bucket).key(key),software.amazon.awssdk.core.sync.RequestBody.fromFile(Paths.get(text(q,"body_path"))));
            Map<String,Object> observed=fields("etag",result.eTag());
            if(result.versionId()!=null) observed.put("version_id",result.versionId());
            return withHttp(observed,result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
        }
        case "get_version": {
            GetObjectRequest.Builder b=GetObjectRequest.builder().bucket(bucket).key(key);
            if(q.has("version_id")) b.versionId(text(q,"version_id"));
            if(q.has("offset")) b.range("bytes="+q.get("offset").asLong()+"-"+(q.get("offset").asLong()+q.get("length").asLong()-1));
            try(software.amazon.awssdk.core.ResponseInputStream<GetObjectResponse> input=client.getObject(b.build())) {
                Files.copy(input,Paths.get(text(q,"output_path")),StandardCopyOption.REPLACE_EXISTING);
                GetObjectResponse result=input.response();
                Map<String,Object> observed=fields("key",key,"size",result.contentLength(),"etag",result.eTag(),"metadata",result.metadata(),"content_type",result.contentType());
                if(result.versionId()!=null) observed.put("version_id",result.versionId());
                if(result.deleteMarker()!=null) observed.put("delete_marker",result.deleteMarker());
                return withHttp(observed,result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
            }
        }
        case "head_version": {
            HeadObjectRequest.Builder b=HeadObjectRequest.builder().bucket(bucket).key(key);
            if(q.has("version_id")) b.versionId(text(q,"version_id"));
            HeadObjectResponse result=client.headObject(b.build());
            Map<String,Object> observed=fields("key",key,"size",result.contentLength(),"etag",result.eTag());
            if(result.lastModified()!=null) observed.put("last_modified_ms",millis(result.lastModified()));
            if(result.versionId()!=null) observed.put("version_id",result.versionId());
            if(result.deleteMarker()!=null) observed.put("delete_marker",result.deleteMarker());
            return withHttp(observed,result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
        }
        case "delete_version": {
            DeleteObjectRequest.Builder b=DeleteObjectRequest.builder().bucket(bucket).key(key);
            if(q.has("version_id")) b.versionId(text(q,"version_id"));
            DeleteObjectResponse result=client.deleteObject(b.build());
            Map<String,Object> observed=new LinkedHashMap<>();
            if(result.versionId()!=null) observed.put("version_id",result.versionId());
            if(result.deleteMarker()!=null) observed.put("delete_marker",result.deleteMarker());
            return withHttp(observed,result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
        }
        case "versions_list": {
            ListObjectVersionsRequest.Builder b=ListObjectVersionsRequest.builder().bucket(bucket);
            if(q.has("prefix")) b.prefix(text(q,"prefix"));
            if(q.has("delimiter")) b.delimiter(text(q,"delimiter"));
            if(q.has("key_marker")) b.keyMarker(text(q,"key_marker"));
            if(q.has("version_id_marker")) b.versionIdMarker(text(q,"version_id_marker"));
            if(q.has("max_keys")) b.maxKeys(q.get("max_keys").asInt());
            ListObjectVersionsResponse result=client.listObjectVersions(b.build());
            List<Map<String,Object>> versions=new ArrayList<>(),markers=new ArrayList<>();
            for(ObjectVersion item:result.versions()) {
                Map<String,Object> row=fields("key",item.key(),"version_id",item.versionId(),"is_latest",item.isLatest(),"size",item.size(),"etag",item.eTag(),"last_modified_ms",millis(item.lastModified()));
                if(item.owner()!=null) row.put("owner",fields("id",item.owner().id(),"display_name",item.owner().displayName()));
                if(item.storageClassAsString()!=null)row.put("storage_class",item.storageClassAsString());
                versions.add(row);
            }
            for(DeleteMarkerEntry item:result.deleteMarkers()) {
                Map<String,Object> row=fields("key",item.key(),"version_id",item.versionId(),"is_latest",item.isLatest(),"last_modified_ms",millis(item.lastModified()));
                if(item.owner()!=null) row.put("owner",fields("id",item.owner().id(),"display_name",item.owner().displayName()));
                markers.add(row);
            }
            List<String> present=new ArrayList<>();
            if(result.hasVersions()) present.add("Versions");if(result.hasDeleteMarkers()) present.add("DeleteMarkers");
            if(result.name()!=null)present.add("Name");
            Map<String,Object> observed=fields("versions",versions,"delete_markers",markers,"fields",present,"truncated",result.isTruncated(),"next_key_marker",result.nextKeyMarker(),"next_version_id_marker",result.nextVersionIdMarker(),"key_marker",result.keyMarker(),"version_id_marker",result.versionIdMarker());
            if(result.name()!=null)observed.put("bucket_name",result.name());
            return withHttp(observed,result.sdkHttpResponse().statusCode(),result.sdkHttpResponse().headers());
        }
        default:throw new UnsupportedOperationException(op);
        }
    }
}
