package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import software.amazon.awssdk.awscore.AwsRequestOverrideConfiguration;

/**
 * Exercises literal Ceph SSE-C/SSE-S3/KMS headers, including intentionally absent fields.
 * Supplies per-request public SDK header overrides without using model setters that
 * can automatically calculate missing MD5 values; the SDK still signs and sends requests.
 */
final class EncryptionHeaders {
    private EncryptionHeaders() {}
    static AwsRequestOverrideConfiguration fromRequest(JsonNode request) {
        if(request.has("encryption_headers") && request.has("sse_headers")) throw new IllegalArgumentException("ambiguous encryption headers");
        return from(request.get(request.has("encryption_headers")?"encryption_headers":"sse_headers"));
    }
    static AwsRequestOverrideConfiguration from(JsonNode input) {
        if (input == null || !input.isObject()) throw new IllegalArgumentException("sse_headers must be an object");
        AwsRequestOverrideConfiguration.Builder builder=AwsRequestOverrideConfiguration.builder();
        input.fields().forEachRemaining(entry -> {
            String name=entry.getKey();
            if (!(name.equals("x-amz-server-side-encryption-customer-algorithm")
                || name.equals("x-amz-server-side-encryption-customer-key")
                || name.equals("x-amz-server-side-encryption-customer-key-md5") || name.equals("content-type") || name.equals("x-amz-server-side-encryption")
                || name.equals("x-amz-server-side-encryption-aws-kms-key-id")) || !entry.getValue().isTextual())
                throw new IllegalArgumentException("invalid encryption header: "+name);
            builder.putHeader(name,entry.getValue().textValue());
        });
        return builder.build();
    }
}
