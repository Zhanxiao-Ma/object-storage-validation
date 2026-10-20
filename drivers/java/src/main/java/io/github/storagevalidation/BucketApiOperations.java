package io.github.storagevalidation;
import java.util.*;
/**
 * Prevents the Java and generated C++ bucket API allowlists from drifting.
 * The pinned API-model generator emits the same explicit operation list for both.
 */
final class BucketApiOperations {
    static final List<String> APIS=Collections.unmodifiableList(Arrays.asList(
        "GetBucketTagging",
        "PutBucketTagging",
        "DeleteBucketTagging",
        "GetBucketCors",
        "PutBucketCors",
        "DeleteBucketCors",
        "GetBucketLifecycleConfiguration",
        "PutBucketLifecycleConfiguration",
        "DeleteBucketLifecycle",
        "GetBucketPolicy",
        "PutBucketPolicy",
        "DeleteBucketPolicy",
        "GetBucketWebsite",
        "PutBucketWebsite",
        "DeleteBucketWebsite",
        "GetBucketEncryption",
        "PutBucketEncryption",
        "DeleteBucketEncryption"));
    static final Map<String,String> JAVA_BINDINGS=Map.ofEntries(Map.entry("GetBucketTagging","GetBucketTagging"),Map.entry("PutBucketTagging","PutBucketTagging"),Map.entry("DeleteBucketTagging","DeleteBucketTagging"),Map.entry("GetBucketCors","GetBucketCors"),Map.entry("PutBucketCors","PutBucketCors"),Map.entry("DeleteBucketCors","DeleteBucketCors"),Map.entry("GetBucketLifecycleConfiguration","GetBucketLifecycleConfiguration"),Map.entry("PutBucketLifecycleConfiguration","PutBucketLifecycleConfiguration"),Map.entry("DeleteBucketLifecycle","DeleteBucketLifecycle"),Map.entry("GetBucketPolicy","GetBucketPolicy"),Map.entry("PutBucketPolicy","PutBucketPolicy"),Map.entry("DeleteBucketPolicy","DeleteBucketPolicy"),Map.entry("GetBucketWebsite","GetBucketWebsite"),Map.entry("PutBucketWebsite","PutBucketWebsite"),Map.entry("DeleteBucketWebsite","DeleteBucketWebsite"),Map.entry("GetBucketEncryption","GetBucketEncryption"),Map.entry("PutBucketEncryption","PutBucketEncryption"),Map.entry("DeleteBucketEncryption","DeleteBucketEncryption"));
}
