package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.*;
import software.amazon.awssdk.core.SdkPojo;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.S3Response;
import static io.github.storagevalidation.WorkerMain.*;

/**
 * Keeps growing bucket-configuration APIs out of the performance-operation switch.
 * Resolves an explicit allowlist to actual SDK request builders and client methods;
 * SDK exceptions and typed response values remain authoritative.
 */
final class S3ApiModels {
    static final List<String> APIS=BucketApiOperations.APIS;

    static boolean handles(String op) { return op.startsWith("api.") && APIS.contains(op.substring(4)); }

    static Map<String,Object> execute(S3Client client,String op,JsonNode q) throws Exception {
        String api=op.substring(4);
        if(!APIS.contains(api))throw new UnsupportedOperationException(op);
        String javaApi=BucketApiOperations.JAVA_BINDINGS.get(api);
        Class<?> requestClass=Class.forName("software.amazon.awssdk.services.s3.model."+javaApi+"Request");
        SdkPojo builder=(SdkPojo)requestClass.getMethod("builder").invoke(null);
        JsonNode configured=q.path("input");
        if(!configured.isObject())throw new IllegalArgumentException("api input must be a JSON object");
        ObjectNode input=((ObjectNode)configured).deepCopy();
        if(q.has("bucket")) {
            if(input.has("Bucket") && !text(input,"Bucket").equals(text(q,"bucket")))throw new IllegalArgumentException("cannot override scoped API bucket");
            input.put("Bucket",text(q,"bucket"));
        }
        Object request=SdkModelJson.read(builder,input);
        Method method=S3Client.class.getMethod(Character.toLowerCase(javaApi.charAt(0))+javaApi.substring(1),requestClass);
        try {
            S3Response response=(S3Response)method.invoke(client,request);
            Map<String,Object> result=fields("data",SdkModelJson.write(response));
            return withHttp(result,response.sdkHttpResponse().statusCode(),response.sdkHttpResponse().headers());
        } catch(InvocationTargetException error) {
            Throwable cause=error.getCause();
            if(cause instanceof Exception)throw (Exception)cause;
            throw new RuntimeException(cause);
        }
    }
}
