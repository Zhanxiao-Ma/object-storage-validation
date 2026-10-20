package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import software.amazon.awssdk.awscore.AwsRequestOverrideConfiguration;

/**
 * Exercises literal Ceph SSE-C headers, including intentionally absent key/MD5 fields.
 * Supplies per-request public SDK header overrides without using model setters that
 * can automatically calculate missing MD5 values; the SDK still signs and sends requests.
 */
final class SseCustomerHeaders {
    private SseCustomerHeaders() {}
    static AwsRequestOverrideConfiguration from(JsonNode input) {
        if (input == null || !input.isObject()) throw new IllegalArgumentException("sse_headers must be an object");
        AwsRequestOverrideConfiguration.Builder builder=AwsRequestOverrideConfiguration.builder();
        input.fields().forEachRemaining(entry -> {
            String name=entry.getKey();
            if (!(name.equals("x-amz-server-side-encryption-customer-algorithm")
                || name.equals("x-amz-server-side-encryption-customer-key")
                || name.equals("x-amz-server-side-encryption-customer-key-md5") || name.equals("content-type")) || !entry.getValue().isTextual())
                throw new IllegalArgumentException("invalid SSE-C header: "+name);
            builder.putHeader(name,entry.getValue().textValue());
        });
        return builder.build();
    }
}
