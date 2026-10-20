package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.*;
import java.util.*;
import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.core.sync.ResponseTransformer;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.*;

/**
 * Preserves Ceph atomic stream interleaving inside actual synchronous SDK IO.
 * Real download sinks call the original writer before an ongoing read resumes;
 * independent SDK clients and byte/size verifiers enforce original object bodies.
 * Upload EOF callbacks will use repeatable SDK input streams rather than RPC batches.
 */
final class S3AtomicStreams {
    static List<String> capabilities() { return Arrays.asList("atomic_stream_read"); }

    /** Generate repeatable constant bytes for actual SDK uploads without retaining a large array; every positive EOF read invokes the supplied original action before returning bytes. */
    static final class ConstantInput extends InputStream {
        final long size;final byte value;final Runnable action;long position,mark;long eofReads;
        ConstantInput(long size,char value,Runnable action) {this.size=size;this.value=(byte)value;this.action=action;}
        public int read() {if(position>=size)return -1;position++;atEnd();return value&255;}
        public int read(byte[] bytes,int offset,int length) {
            Objects.checkFromIndexSize(offset,length,bytes.length);
            if(length==0)return 0;if(position>=size)return -1;
            int count=(int)Math.min(length,size-position);Arrays.fill(bytes,offset,offset+count,value);position+=count;atEnd();return count;
        }
        private void atEnd() {if(position==size){eofReads++;if(action!=null)action.run();}}
        public boolean markSupported(){return true;}
        public void mark(int limit){mark=position;}
        public void reset(){position=mark;}
        public long skip(long count){long skipped=Math.min(Math.max(0,count),size-position);position+=skipped;return skipped;}
    }

    /** Verify each actual SDK sink write, execute a first-positive-write action synchronously, and check the original expected full length at completion. */
    static final class VerifyingOutput extends OutputStream {
        final long expected;final byte value;final Runnable action;long count,writes,callbacks;boolean interrupted;RuntimeException failure;
        VerifyingOutput(long size,char value,Runnable action){expected=size;this.value=(byte)value;this.action=action;}
        public void write(int value){write(new byte[]{(byte)value},0,1);}
        public void write(byte[] data,int offset,int length){
            try {
                Objects.checkFromIndexSize(offset,length,data.length);
                for(int i=offset;i<offset+length;i++)if(data[i]!=value)throw new IllegalStateException("atomic download byte differs at "+(count+i-offset));
                count+=length;if(length>0)writes++;
                if(!interrupted && action!=null && count>0){action.run();interrupted=true;callbacks++;}
            } catch(RuntimeException error){failure=error;throw error;}
        }
        void finish(){if(count!=expected)throw new IllegalStateException("atomic download size "+count+" != "+expected);}
    }

    static PutObjectResponse put(S3Client client,String bucket,String key,long size,char value) {
        return client.putObject(PutObjectRequest.builder().bucket(bucket).key(key).contentLength(size).build(),
            RequestBody.fromContentProvider(()->new ConstantInput(size,value,null),size,"application/octet-stream"));
    }
    static VerifyingOutput verify(S3Client client,String bucket,String key,long size,char value,Runnable action) {
        VerifyingOutput sink=new VerifyingOutput(size,value,action);
        try {client.getObject(GetObjectRequest.builder().bucket(bucket).key(key).build(),ResponseTransformer.toOutputStream(sink));}
        catch(RuntimeException error){if(sink.failure!=null)throw sink.failure;throw error;}
        sink.finish();return sink;
    }
    static Map<String,Object> execute(S3Client original,JsonNode q) {
        if(!q.path("mode").isTextual() || !q.path("mode").textValue().equals("read"))throw new IllegalArgumentException("atomic stream mode not implemented");
        if(!q.has("size") || !q.get("size").isIntegralNumber() || !q.get("size").canConvertToLong() || q.get("size").longValue()<1)throw new IllegalArgumentException("atomic size must be a positive integer");
        if(q.has("identity") && (!q.get("identity").isTextual() || !q.get("identity").textValue().equals("main")))throw new IllegalArgumentException("atomic source requires main identity");
        long size=q.get("size").longValue();String bucket=text(q,"bucket"),key=text(q,"key");
        put(original,bucket,key,size,'A');
        VerifyingOutput ongoing;
        try(S3Identities readers=new S3Identities()) {
            ongoing=verify(readers.get("main"),bucket,key,size,'A',()->put(original,bucket,key,size,'B'));
        }
        try(S3Identities verifier=new S3Identities()) {
            VerifyingOutput finalBody=verify(verifier.get("main"),bucket,key,size,'B',null);
            Map<String,Object> out=new LinkedHashMap<>();out.put("read_size",ongoing.count);out.put("read_writes",ongoing.writes);
            out.put("callbacks",ongoing.callbacks);out.put("final_size",finalBody.count);out.put("mode","read");return out;
        }
    }
    static String text(JsonNode q,String name){if(!q.has(name)||!q.get(name).isTextual()||q.get(name).textValue().isEmpty())throw new IllegalArgumentException(name+" must be a nonempty string");return q.get(name).textValue();}
}
