package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.*;
import software.amazon.awssdk.auth.credentials.*;
import software.amazon.awssdk.services.s3.S3Client;
import static io.github.storagevalidation.WorkerMain.*;

/**
 * Supports Ceph authorization scenarios that must use distinct real SDK identities.
 * Lazily caches isolated clients with the same endpoint/transport settings and explicit
 * per-identity credential providers; anonymous requests never fall back to main credentials.
 * Credentials remain in environment variables and are never returned in worker metadata.
 */
final class S3Identities implements AutoCloseable {
    private final Map<String,S3Client> clients=new LinkedHashMap<>();
    static final List<String> NAMED=Arrays.asList("alt","tenant","bad_auth");

    static List<String> capabilities() {
        List<String> result=new ArrayList<>(Arrays.asList("identity_main","identity_anonymous","wire_headers","checksum_when_required"));
        for(String name:NAMED) {
            String prefix=name.toUpperCase(Locale.ROOT)+"_";
            if(!env(prefix+"ACCESS_KEY","").isEmpty() && !env(prefix+"SECRET_KEY","").isEmpty()) result.add("identity_"+name);
        }
        return result;
    }

    S3Client select(JsonNode request) {
        // Pinned Java SDK 2.20.162 predates default optional checksum trailers.
        // Its normal request behavior already satisfies WHEN_REQUIRED; real wire tests
        // guard this mapping so a future SDK upgrade cannot silently enable trailers.
        if(request.has("checksum_when_required") && (!request.get("checksum_when_required").isBoolean() || !request.get("checksum_when_required").booleanValue()))
            throw new IllegalArgumentException("checksum_when_required must be true");
        if(!request.has("identity")) return get("main",request.has("wire_headers"));
        if(!request.get("identity").isTextual())throw new IllegalArgumentException("identity must be a string");
        return get(request.get("identity").textValue(),request.has("wire_headers"));
    }

    S3Client get(String name) { return get(name,false); }

    S3Client get(String name,boolean headers) {
        if(!name.equals("main") && !name.equals("anonymous") && !NAMED.contains(name))throw new IllegalArgumentException("unsupported identity");
        return clients.computeIfAbsent(name+(headers?":wire":""),ignored->create(name,headers));
    }

    private static S3Client create(String name,boolean headers) {
        AwsCredentialsProvider provider;
        if(name.equals("anonymous"))provider=AnonymousCredentialsProvider.create();
        else {
            String prefix=name.equals("main")?"":name.toUpperCase(Locale.ROOT)+"_";
            String token=env(prefix+"SESSION_TOKEN","");
            AwsCredentials credentials=token.isEmpty()?AwsBasicCredentials.create(required(prefix+"ACCESS_KEY"),required(prefix+"SECRET_KEY"))
                :AwsSessionCredentials.create(required(prefix+"ACCESS_KEY"),required(prefix+"SECRET_KEY"),token);
            provider=StaticCredentialsProvider.create(credentials);
        }
        return S3Client.builder()
            .endpointOverride(java.net.URI.create(required("ENDPOINT")))
            .region(software.amazon.awssdk.regions.Region.of(required("REGION")))
            .credentialsProvider(provider)
            .serviceConfiguration(software.amazon.awssdk.services.s3.S3Configuration.builder()
                .pathStyleAccessEnabled(Boolean.parseBoolean(env("PATH_STYLE","false"))).build())
            .overrideConfiguration(c->{
                c.apiCallTimeout(java.time.Duration.ofMillis(headers?Long.parseLong(env("HEADER_REQUEST_TIMEOUT_MS","30000")):30000))
                    .retryPolicy(software.amazon.awssdk.core.retry.RetryPolicy.builder().numRetries(Integer.parseInt(env("RETRIES","0"))).build());
                if(headers)c.addExecutionInterceptor(new WireHeaders());
            })
            .httpClientBuilder(software.amazon.awssdk.http.apache.ApacheHttpClient.builder().maxConnections(Integer.parseInt(env("CONNECTIONS","16"))))
            .build();
    }

    public void close() { for(S3Client client:clients.values())client.close();clients.clear(); }
}
