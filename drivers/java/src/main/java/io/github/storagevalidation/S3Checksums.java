package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.util.*;
import software.amazon.awssdk.core.SdkPojo;
import software.amazon.awssdk.core.SdkField;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;
import software.amazon.awssdk.services.s3.model.CreateMultipartUploadRequest;

/**
 * Preserves explicit checksum inputs and optional native SDK response fields across SDK versions.
 * Uses public SDK field descriptors to reject unsupported typed members before IO, and reports
 * only fields decoded by the real SDK, without fabricating support from raw request headers.
 */
final class S3Checksums {
    private static final Set<String> FIELDS=new LinkedHashSet<>(Arrays.asList("ChecksumAlgorithm","ChecksumType","ChecksumCRC32","ChecksumCRC32C","ChecksumCRC64NVME","ChecksumSHA1","ChecksumSHA256"));
    static void apply(SdkPojo builder,JsonNode request) {
        ObjectNode input=JsonNodeFactory.instance.objectNode();
        if(request.has("checksum")) {
            JsonNode checksum=request.get("checksum");
            if(!checksum.isObject()) throw new IllegalArgumentException("checksum must be an object");
            checksum.fields().forEachRemaining(entry->{
                if(!FIELDS.contains(entry.getKey())) throw new IllegalArgumentException("unknown checksum member "+entry.getKey());
                if(!entry.getValue().isTextual()) throw new IllegalArgumentException("checksum member must be textual: "+entry.getKey());
                input.set(entry.getKey(),entry.getValue());
            });
        }
        if(request.has("checksum_mode")) {
            if(!request.get("checksum_mode").isTextual()) throw new IllegalArgumentException("checksum_mode must be textual");
            input.set("ChecksumMode",request.get("checksum_mode"));
        }
        // The shared reader mutates these actual SDK builder fields before building.
        if(input.size()>0) SdkModelJson.read(builder,input);
    }
    static Map<String,Object> observe(Map<String,Object> existing,SdkPojo response) {
        Map<String,Object> checksum=new LinkedHashMap<>();
        SdkModelJson.write(response).forEach((name,value)->{if(FIELDS.contains(name))checksum.put(name,value);});
        existing.put("checksum",checksum);return existing;
    }
    static boolean has(SdkPojo model,String name) {
        for(SdkField<?> field:model.sdkFields())if(name.equals(field.memberName()))return true;
        return false;
    }
    static List<String> capabilities() {
        List<String> result=new ArrayList<>();SdkPojo put=PutObjectRequest.builder().build();
        for(String algorithm:Arrays.asList("SHA256","SHA1","CRC32","CRC32C","CRC64NVME"))
            if(has(put,"Checksum"+algorithm))result.add("checksum_"+algorithm.toLowerCase(Locale.ROOT));
        if(has(CreateMultipartUploadRequest.builder().build(),"ChecksumType"))result.add("checksum_type");
        return result;
    }
}
