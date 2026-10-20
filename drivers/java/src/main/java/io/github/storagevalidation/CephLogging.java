package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;
import javax.xml.transform.*;
import javax.xml.transform.dom.DOMSource;
import javax.xml.transform.stream.StreamResult;
import org.w3c.dom.*;
import software.amazon.awssdk.core.interceptor.*;
import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.http.*;
import software.amazon.awssdk.services.s3.S3Client;

/**
 * Express Ceph logging extensions missing from the pinned AWS S3 model.
 * Keep official typed marshalling, signing, transport and response decoding;
 * patch only named extension elements in the SDK-produced XML before signing.
 */
final class CephLogging implements ExecutionInterceptor {
    private static final Set<String> EXTENSIONS=Set.of("LoggingType","ObjectRollTime","RecordsBatchSize","Filter","TargetObjectKeyFormat");
    private static final ThreadLocal<State> ACTIVE=new ThreadLocal<>();
    /** Prevent extension XML or POST settings leaking between SDK calls by keeping each call's fields and final bytes in thread-local state that is restored on success and failure. */
    private static final class State { JsonNode extensions;boolean post;byte[] bytes;State(JsonNode e,boolean p){extensions=e;post=p;} }
    static boolean handles(String op){return Set.of("api.ceph.PutBucketLogging","api.ceph.GetBucketLogging","api.ceph.PostBucketLogging").contains(op);}
    static List<String> capabilities(){return List.of("ceph_logging_wire_extensions");}
    static void object(JsonNode node,Set<String> keys) {
        if(!node.isObject())throw new IllegalArgumentException("logging structure must be an object");
        node.fieldNames().forEachRemaining(k->{if(!keys.contains(k))throw new IllegalArgumentException("unknown logging field: "+k);});
    }
    static void string(JsonNode n){if(!n.isTextual())throw new IllegalArgumentException("logging value must be a string");}
    static void validate(JsonNode e) {
        object(e,EXTENSIONS);
        for(String k:List.of("LoggingType"))if(e.has(k))string(e.get(k));
        for(String k:List.of("ObjectRollTime","RecordsBatchSize"))if(e.has(k) && (!e.get(k).isIntegralNumber() || !e.get(k).canConvertToLong()))throw new IllegalArgumentException("logging integer must fit int64");
        if(e.has("Filter")) {
            JsonNode f=e.get("Filter");object(f,Set.of("Key"));
            if(f.has("Key")) {JsonNode key=f.get("Key");object(key,Set.of("FilterRules"));if(key.has("FilterRules")) {
                if(!key.get("FilterRules").isArray())throw new IllegalArgumentException("FilterRules must be an array");
                for(JsonNode rule:key.get("FilterRules")){object(rule,Set.of("Name","Value"));for(String k:List.of("Name","Value"))if(rule.has(k))string(rule.get(k));}
            }}
        }
        if(e.has("TargetObjectKeyFormat")) {
            JsonNode format=e.get("TargetObjectKeyFormat");object(format,Set.of("SimplePrefix","PartitionedPrefix"));
            if(format.has("SimplePrefix"))object(format.get("SimplePrefix"),Set.of());
            if(format.has("PartitionedPrefix")){JsonNode part=format.get("PartitionedPrefix");object(part,Set.of("PartitionDateSource"));if(part.has("PartitionDateSource"))string(part.get("PartitionDateSource"));}
        }
    }
    static Map<String,Object> execute(S3Client client,String op,JsonNode q) throws Exception {
        if(!handles(op))throw new UnsupportedOperationException(op);
        if(!q.path("input").isObject())throw new IllegalArgumentException("api input must be an object");
        ObjectNode input=((ObjectNode)q.get("input")).deepCopy();
        ObjectNode extensions=input.objectNode();
        boolean put=op.equals("api.ceph.PutBucketLogging");
        if(put && input.has("BucketLoggingStatus")) {
            JsonNode status=input.get("BucketLoggingStatus");object(status,Set.of("LoggingEnabled"));
            if(status.has("LoggingEnabled")) {
                JsonNode enabled=status.get("LoggingEnabled");object(enabled,Set.of("TargetBucket","TargetPrefix","TargetGrants","LoggingType","ObjectRollTime","RecordsBatchSize","Filter","TargetObjectKeyFormat"));
                for(String k:EXTENSIONS)if(enabled.has(k))extensions.set(k,((ObjectNode)enabled).remove(k));
            }
        }
        validate(extensions);
        ObjectNode forwarded=((ObjectNode)q).deepCopy();forwarded.set("input",input);
        State previous=ACTIVE.get();ACTIVE.set(new State(extensions,op.equals("api.ceph.PostBucketLogging")));
        try {return S3ApiModels.execute(client,put?"api.PutBucketLogging":"api.GetBucketLogging",forwarded);}
        finally {if(previous==null)ACTIVE.remove();else ACTIVE.set(previous);}
    }
    private static Element append(Document doc,Element parent,String name,String value) {
        Element n=doc.createElementNS(parent.getNamespaceURI(),name);if(value!=null)n.appendChild(doc.createTextNode(value));parent.appendChild(n);return n;
    }
    private static void extension(Document d,Element parent,String name,JsonNode node) {
        if(node.isValueNode()){append(d,parent,name,node.asText());return;}
        Element current=append(d,parent,name,null);
        node.fields().forEachRemaining(entry->{
            String child=entry.getKey().equals("Key")?"S3Key":entry.getKey();
            if(entry.getKey().equals("FilterRules")){for(JsonNode rule:entry.getValue())extension(d,current,"FilterRule",rule);}
            else extension(d,current,child,entry.getValue());
        });
    }
    public void afterMarshalling(Context.AfterMarshalling context,ExecutionAttributes attrs) {
        State s=ACTIVE.get();if(s==null || s.post || s.extensions.isEmpty())return;
        try {
            byte[] original;
            try(InputStream stream=context.requestBody().orElseThrow().contentStreamProvider().newStream()){original=stream.readAllBytes();}
            DocumentBuilderFactory factory=DocumentBuilderFactory.newInstance();factory.setNamespaceAware(true);
            factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl",true);factory.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING,true);
            Document d=factory.newDocumentBuilder().parse(new ByteArrayInputStream(original));
            NodeList enabled=d.getElementsByTagNameNS("*","LoggingEnabled");if(enabled.getLength()!=1)throw new IllegalArgumentException("SDK logging payload lacks LoggingEnabled");
            s.extensions.fields().forEachRemaining(entry->extension(d,(Element)enabled.item(0),entry.getKey(),entry.getValue()));
            TransformerFactory tf=TransformerFactory.newInstance();tf.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING,true);
            Transformer t=tf.newTransformer();t.setOutputProperty(OutputKeys.OMIT_XML_DECLARATION,"yes");t.setOutputProperty(OutputKeys.ENCODING,"UTF-8");
            ByteArrayOutputStream bytes=new ByteArrayOutputStream();t.transform(new DOMSource(d),new StreamResult(bytes));s.bytes=bytes.toByteArray();
        }catch(Exception error){throw new IllegalArgumentException("cannot extend official logging payload",error);}
    }
    public SdkHttpRequest modifyHttpRequest(Context.ModifyHttpRequest context,ExecutionAttributes attrs) {
        State s=ACTIVE.get();if(s==null)return context.httpRequest();
        SdkHttpRequest.Builder b=context.httpRequest().toBuilder();
        if(s.post)b.method(SdkHttpMethod.POST);
        if(s.bytes!=null) {
            try {b.putHeader("Content-MD5",Base64.getEncoder().encodeToString(MessageDigest.getInstance("MD5").digest(s.bytes)));}
            catch(Exception error){throw new IllegalStateException(error);}
            b.putHeader("Content-Length",Integer.toString(s.bytes.length));
            b.removeHeader("x-amz-content-sha256");
        }
        return b.build();
    }
    public Optional<RequestBody> modifyHttpContent(Context.ModifyHttpRequest context,ExecutionAttributes attrs) {
        State s=ACTIVE.get();return s!=null && s.bytes!=null?Optional.of(RequestBody.fromBytes(s.bytes)):context.requestBody();
    }
}
