package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.*;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import software.amazon.awssdk.auth.credentials.*;
import software.amazon.awssdk.core.SdkPojo;
import software.amazon.awssdk.services.s3.*;
import software.amazon.awssdk.services.s3.model.*;
import static io.github.storagevalidation.WorkerMain.*;

/**
 * Exposes genuine S3 Select event streams to language-neutral Ceph scenarios.
 * Uses the official asynchronous S3 client and event visitor, persists each actual
 * Records payload without UTF-8 conversion, and retains event order and native
 * failures so the Python consumer preserves source iterator error boundaries.
 */
final class S3Select {
    static Map<String,Object> execute(JsonNode q) throws Exception {
        if(q.has("identity") && !q.get("identity").asText().equals("main"))throw new IllegalArgumentException("Select source requires main identity");
        SelectObjectContentRequest request=(SelectObjectContentRequest)SdkModelJson.read((SdkPojo)SelectObjectContentRequest.builder(),q.get("input"));
        Path path=Paths.get(q.get("output_path").asText());
        List<Map<String,Object>> events=Collections.synchronizedList(new ArrayList<>());
        AtomicReference<SelectObjectContentResponse> response=new AtomicReference<>();
        AtomicReference<Throwable> failure=new AtomicReference<>();
        AtomicLong offset=new AtomicLong();
        String token=env("SESSION_TOKEN","");
        AwsCredentials credentials=token.isEmpty()?AwsBasicCredentials.create(required("ACCESS_KEY"),required("SECRET_KEY")):AwsSessionCredentials.create(required("ACCESS_KEY"),required("SECRET_KEY"),token);
        long requestTimeout=S3Identities.timeout("REQUEST_TIMEOUT_MS",30000);
        long connectTimeout=S3Identities.timeout("CONNECT_TIMEOUT_MS",2000);
        software.amazon.awssdk.http.nio.netty.NettyNioAsyncHttpClient.Builder http=software.amazon.awssdk.http.nio.netty.NettyNioAsyncHttpClient.builder()
                .maxConcurrency(Integer.parseInt(env("CONNECTIONS","16")))
                .connectionTimeout(java.time.Duration.ofMillis(connectTimeout))
                .readTimeout(java.time.Duration.ofMillis(requestTimeout)).writeTimeout(java.time.Duration.ofMillis(requestTimeout));
        java.net.URI proxy=S3Identities.httpProxy();
        if(proxy!=null)http.proxyConfiguration(software.amazon.awssdk.http.nio.netty.ProxyConfiguration.builder()
                .scheme("http").host(proxy.getHost()).port(proxy.getPort()<0?80:proxy.getPort())
                .nonProxyHosts(Collections.emptySet()).useSystemPropertyValues(false).build());
        try(OutputStream output=Files.newOutputStream(path);S3AsyncClient client=S3AsyncClient.builder()
                .endpointOverride(java.net.URI.create(required("ENDPOINT")))
                .region(software.amazon.awssdk.regions.Region.of(required("REGION")))
                .credentialsProvider(StaticCredentialsProvider.create(credentials))
                .serviceConfiguration(S3Configuration.builder().pathStyleAccessEnabled(Boolean.parseBoolean(env("PATH_STYLE","false"))).build())
                .overrideConfiguration(c->c.apiCallTimeout(java.time.Duration.ofMillis(requestTimeout)).retryPolicy(software.amazon.awssdk.core.retry.RetryPolicy.builder().numRetries(Integer.parseInt(env("RETRIES","0"))).build()))
                .httpClientBuilder(http)
                .build()) {
            SelectObjectContentResponseHandler handler=SelectObjectContentResponseHandler.builder()
                .onResponse(response::set)
                .onError(error->failure.compareAndSet(null,error))
                .subscriber(SelectObjectContentResponseHandler.Visitor.builder()
                    .onRecords(event->{byte[] bytes=event.payload().asByteArray();try {output.write(bytes);}catch(IOException error){throw new UncheckedIOException(error);}events.add(new LinkedHashMap<>(Map.of("type","Records","offset",offset.getAndAdd(bytes.length),"length",bytes.length)));})
                    .onStats(event->events.add(new LinkedHashMap<>(Map.of("type","Stats","Details",SdkModelJson.write(event.details())))))
                    .onProgress(event->events.add(new LinkedHashMap<>(Map.of("type","Progress","Details",SdkModelJson.write(event.details())))))
                    .onCont(event->events.add(new LinkedHashMap<>(Map.of("type","Cont"))))
                    .onEnd(event->events.add(new LinkedHashMap<>(Map.of("type","End")))).build()).build();
            try {client.selectObjectContent(request,handler).join();}
            catch(CompletionException error){failure.compareAndSet(null,error.getCause());}
            Throwable error=failure.get();
            while((error instanceof CompletionException || error instanceof ExecutionException) && error.getCause()!=null)error=error.getCause();
            if(response.get()==null && error!=null){if(error instanceof RuntimeException)throw (RuntimeException)error;throw new RuntimeException(error);}
            if(error!=null){Map<String,Object> nativeError=new LinkedHashMap<>();nativeError.put("type","Error");nativeError.put("error_message",error.getMessage());nativeError.put("error_class",error.getClass().getName());nativeError.put("error_kind",error instanceof S3Exception?"service":"decoder");if(error instanceof S3Exception){S3Exception e=(S3Exception)error;nativeError.put("status_code",e.statusCode());if(e.awsErrorDetails()!=null)nativeError.put("error_code",e.awsErrorDetails().errorCode());}events.add(nativeError);}
            Map<String,Object> result=new LinkedHashMap<>();result.put("events",events);result.put("records_size",offset.get());result.put("status_code",response.get()==null?null:response.get().sdkHttpResponse().statusCode());result.put("http_headers",response.get()==null?Collections.emptyMap():response.get().sdkHttpResponse().headers());return result;
        }
    }
}
