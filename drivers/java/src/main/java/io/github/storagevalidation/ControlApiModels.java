package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.net.URI;
import java.time.Duration;
import java.util.*;
import software.amazon.awssdk.auth.credentials.*;
import software.amazon.awssdk.core.SdkPojo;
import software.amazon.awssdk.core.SdkResponse;
import software.amazon.awssdk.core.client.config.ClientOverrideConfiguration;
import software.amazon.awssdk.core.retry.RetryPolicy;
import software.amazon.awssdk.http.apache.ApacheHttpClient;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.iam.IamClient;
import software.amazon.awssdk.services.sts.StsClient;
import static io.github.storagevalidation.WorkerMain.*;

/**
 * Executes service-specific IAM and STS compatibility operations without routing them
 * through S3. Explicit service endpoints, signing regions and independent credential
 * identities configure cached official clients; public SDK model descriptors preserve
 * typed inputs, response presence and epoch-millisecond timestamps.
 */
final class ControlApiModels implements AutoCloseable {
    private static final List<String> IDENTITIES=List.of("iam","iam_root","iam_alt_root");
    private final Map<String,software.amazon.awssdk.core.SdkClient> clients=new LinkedHashMap<>();

    static boolean handles(String op) {
        return op.startsWith("api.iam.") && ControlApiOperations.IAM.contains(op.substring(8))
            || op.startsWith("api.sts.") && ControlApiOperations.STS.contains(op.substring(8));
    }

    static List<String> capabilities() {
        List<String> result=new ArrayList<>();
        for(String service:List.of("iam","sts")) {
            for(String api:service.equals("iam")?ControlApiOperations.IAM:ControlApiOperations.STS)result.add("api."+service+"."+api);
            String prefix=service.toUpperCase(Locale.ROOT)+"_";
            if(env(prefix+"ENDPOINT","").isEmpty() || env(prefix+"REGION","").isEmpty())continue;
            for(String identity:IDENTITIES) {
                String credentials=identity.toUpperCase(Locale.ROOT)+"_";
                if(!env(credentials+"ACCESS_KEY","").isEmpty() && !env(credentials+"SECRET_KEY","").isEmpty())
                    result.add((service.equals("iam")?"identity_":"identity_sts_")+identity);
            }
        }
        return result;
    }

    Map<String,Object> execute(String op,JsonNode q) throws Exception {
        if(!handles(op))throw new UnsupportedOperationException("unsupported control operation");
        if(q.has("wire_headers") || q.has("wire_query") || q.has("checksum_when_required"))throw new IllegalArgumentException("S3 wire extensions are not control API fields");
        String service=op.substring(4,7),api=op.substring(8);
        String identity=q.has("identity")?text(q,"identity"):"iam";
        if(!IDENTITIES.contains(identity))throw new IllegalArgumentException("unsupported control identity");
        // Java IAM names OpenId using the SDK's own class and method spelling.
        String javaApi=api.replace("OpenID","OpenId").replace("ClientID","ClientId");
        String packageName="software.amazon.awssdk.services."+service;
        Class<?> requestClass=Class.forName(packageName+".model."+javaApi+"Request");
        SdkPojo builder=(SdkPojo)requestClass.getMethod("builder").invoke(null);
        Object request=SdkModelJson.read(builder,q.path("input"));
        Object client=get(service,identity);
        Class<?> clientClass=service.equals("iam")?IamClient.class:StsClient.class;
        // SDK request class spelling (OpenId/ClientId) differs from public
        // client method spelling (OpenID/ClientID). Resolve the unique real
        // synchronous response method by its exact official request type;
        // Consumer builders and paginator overloads are deliberately excluded.
        Method method=null;
        for(Method candidate:clientClass.getMethods()) {
            if(candidate.getParameterCount()==1 && candidate.getParameterTypes()[0]==requestClass
                && SdkResponse.class.isAssignableFrom(candidate.getReturnType())) {
                if(method!=null)throw new IllegalStateException("ambiguous official SDK control request binding");
                method=candidate;
            }
        }
        if(method==null)throw new IllegalStateException("official SDK control request binding unavailable");
        try {
            SdkResponse response=(SdkResponse)method.invoke(client,request);
            Map<String,Object> data=SdkModelJson.write((SdkPojo)response);
            return withHttp(fields("data",data),response.sdkHttpResponse().statusCode(),response.sdkHttpResponse().headers());
        } catch(InvocationTargetException error) {
            Throwable cause=error.getCause();
            if(cause instanceof Exception)throw (Exception)cause;
            throw new RuntimeException(cause);
        }
    }

    private synchronized software.amazon.awssdk.core.SdkClient get(String service,String identity) {
        return clients.computeIfAbsent(service+":"+identity,ignored->create(service,identity));
    }

    private static software.amazon.awssdk.core.SdkClient create(String service,String identity) {
        String prefix=service.toUpperCase(Locale.ROOT)+"_",credentials=identity.toUpperCase(Locale.ROOT)+"_";
        URI endpoint=URI.create(required(prefix+"ENDPOINT"));
        if(!List.of("http","https").contains(endpoint.getScheme()) || endpoint.getHost()==null || endpoint.getRawUserInfo()!=null || endpoint.getRawQuery()!=null || endpoint.getRawFragment()!=null)
            throw new IllegalArgumentException("control endpoint must be an explicit HTTP(S) URL without credentials/query/fragment");
        Region region=Region.of(required(prefix+"REGION"));
        String access=required(credentials+"ACCESS_KEY"),secret=required(credentials+"SECRET_KEY"),token=env(credentials+"SESSION_TOKEN","");
        AwsCredentials actual=token.isEmpty()?AwsBasicCredentials.create(access,secret):AwsSessionCredentials.create(access,secret,token);
        StaticCredentialsProvider provider=StaticCredentialsProvider.create(actual);
        long timeout=positive("REQUEST_TIMEOUT_MS",30000),connect=positive("CONNECT_TIMEOUT_MS",2000);
        ClientOverrideConfiguration override=ClientOverrideConfiguration.builder().apiCallTimeout(Duration.ofMillis(timeout))
            .retryPolicy(RetryPolicy.builder().numRetries(Integer.parseInt(env("RETRIES","0"))).build()).build();
        if(service.equals("iam"))return IamClient.builder().endpointOverride(endpoint).region(region).credentialsProvider(provider).overrideConfiguration(override)
            .httpClientBuilder(ApacheHttpClient.builder().maxConnections((int)positive("CONNECTIONS",16)).connectionTimeout(Duration.ofMillis(connect)).socketTimeout(Duration.ofMillis(timeout))).build();
        return StsClient.builder().endpointOverride(endpoint).region(region).credentialsProvider(provider).overrideConfiguration(override)
            .httpClientBuilder(ApacheHttpClient.builder().maxConnections((int)positive("CONNECTIONS",16)).connectionTimeout(Duration.ofMillis(connect)).socketTimeout(Duration.ofMillis(timeout))).build();
    }

    private static long positive(String key,long fallback) {
        String raw=env(key,Long.toString(fallback));
        if(!raw.matches("[0-9]+"))throw new IllegalArgumentException(key+" must be a positive integer");
        long n=Long.parseLong(raw);if(n<1 || n>Integer.MAX_VALUE)throw new IllegalArgumentException(key+" outside 1..2147483647");return n;
    }

    // Keep the official SDK's exact policy string. The shared Python RPC consumer
    // decodes policies after receiving it, preserving JSON insertion order that C++
    // map-backed JSON objects would otherwise irreversibly sort.

    public synchronized void close() { for(software.amazon.awssdk.core.SdkClient client:clients.values())client.close();clients.clear(); }
}
