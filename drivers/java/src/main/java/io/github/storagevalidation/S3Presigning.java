package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import java.net.URI;
import java.time.Duration;
import java.util.*;
import software.amazon.awssdk.auth.credentials.*;
import software.amazon.awssdk.awscore.presigner.PresignedRequest;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Configuration;
import software.amazon.awssdk.services.s3.model.*;
import software.amazon.awssdk.services.s3.presigner.S3Presigner;
import software.amazon.awssdk.services.s3.presigner.model.*;
import static io.github.storagevalidation.WorkerMain.*;

/**
 * Produces actual AWS SDK presigned requests for original Ceph HTTP consumers.
 * Use the official S3Presigner with explicit isolated main/tenant credentials and
 * identical endpoint/addressing configuration; return its URL/headers unchanged.
 * SDK duration validation is allowed to fail locally and is never recast as HTTP.
 */
final class S3Presigning {
    static List<String> capabilities() { return Arrays.asList("presign", "presign_v4"); }
    static Map<String,Object> execute(JsonNode q) {
        if(q.has("signature_version") && !q.path("signature_version").asText().equals("v4"))
            throw new IllegalArgumentException("official Java S3Presigner supports v4 only");
        if(q.has("identity") && !q.get("identity").isTextual())throw new IllegalArgumentException("identity must be a string");
        String identity=q.path("identity").asText("main");
        if(!identity.equals("main") && !identity.equals("tenant"))throw new IllegalArgumentException("presign identity must be main or tenant");
        String prefix=identity.equals("main")?"":"TENANT_";
        String token=env(prefix+"SESSION_TOKEN","");
        AwsCredentials credentials=token.isEmpty()?AwsBasicCredentials.create(required(prefix+"ACCESS_KEY"),required(prefix+"SECRET_KEY"))
            :AwsSessionCredentials.create(required(prefix+"ACCESS_KEY"),required(prefix+"SECRET_KEY"),token);
        String method=text(q,"method");
        if(!method.equals("GET") && !method.equals("PUT"))throw new IllegalArgumentException("presign method must be GET or PUT");
        if(q.has("expires_seconds") && (!q.get("expires_seconds").isIntegralNumber() || !q.get("expires_seconds").canConvertToLong()))
            throw new IllegalArgumentException("expires_seconds must be an integer");
        if(q.has("acl") && (!q.get("acl").isTextual() || !method.equals("PUT")))throw new IllegalArgumentException("acl requires PUT and a string");
        // Java's official presign model requires an explicit Duration. Preserve
        // omission in metadata and use the source boto default of one hour.
        Duration duration=Duration.ofSeconds(q.has("expires_seconds")?q.get("expires_seconds").longValue():3600);
        try(S3Presigner signer=S3Presigner.builder().endpointOverride(URI.create(required("ENDPOINT")))
            .region(Region.of(required("REGION"))).credentialsProvider(StaticCredentialsProvider.create(credentials))
            .serviceConfiguration(S3Configuration.builder().pathStyleAccessEnabled(Boolean.parseBoolean(env("PATH_STYLE","false"))).build()).build()) {
            PresignedRequest result;
            if(method.equals("GET"))result=signer.presignGetObject(GetObjectPresignRequest.builder().signatureDuration(duration)
                .getObjectRequest(GetObjectRequest.builder().bucket(text(q,"bucket")).key(text(q,"key")).build()).build());
            else {
                PutObjectRequest.Builder input=PutObjectRequest.builder().bucket(text(q,"bucket")).key(text(q,"key"));
                if(q.has("acl"))input.acl(q.get("acl").textValue());
                result=signer.presignPutObject(PutObjectPresignRequest.builder().signatureDuration(duration).putObjectRequest(input.build()).build());
            }
            Map<String,Object> output=new LinkedHashMap<>();
            output.put("url",result.url().toString());output.put("signed_headers",result.signedHeaders());
            output.put("signature_version","v4");output.put("expires_input_present",q.has("expires_seconds"));
            output.put("expiration_epoch_ms",result.expiration().toEpochMilli());return output;
        }
    }
    private static String text(JsonNode q,String name) {
        if(!q.has(name) || !q.get(name).isTextual() || q.get(name).textValue().isEmpty())throw new IllegalArgumentException(name+" must be a non-empty string");
        return q.get(name).textValue();
    }
}
