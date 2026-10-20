package io.github.storagevalidation;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.*;
import java.util.*;
import java.nio.charset.StandardCharsets;
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
    static List<String> capabilities() {
        List<String> out=new ArrayList<>(Arrays.asList("atomic_stream_read","atomic_stream_write","atomic_stream_conditional_write","atomic_versions_overlap"));
        if(Integer.parseInt(WorkerMain.env("CONNECTIONS","16"))>=2)out.addAll(Arrays.asList("atomic_stream_dual_write","atomic_stream_multipart_resend","atomic_stream_dual_conditional_write","atomic_stream_bucket_gone"));
        return out;
    }

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
        String mode=text(q,"mode");
        if(!Arrays.asList("read","write","dual_write","conditional_write","multipart_resend","dual_conditional_write","bucket_gone","versioned_overlap").contains(mode))throw new IllegalArgumentException("atomic stream mode not implemented");
        if((mode.equals("dual_write") || mode.equals("multipart_resend") || mode.equals("dual_conditional_write") || mode.equals("bucket_gone")) && Integer.parseInt(WorkerMain.env("CONNECTIONS","16"))<2)throw new IllegalArgumentException("same-client nested upload requires OSV_CONNECTIONS >= 2");
        if(!q.has("size") || !q.get("size").isIntegralNumber() || !q.get("size").canConvertToLong() || q.get("size").longValue()<1)throw new IllegalArgumentException("atomic size must be a positive integer");
        if(q.has("identity") && (!q.get("identity").isTextual() || !q.get("identity").textValue().equals("main")))throw new IllegalArgumentException("atomic source requires main identity");
        long size=q.get("size").longValue();String bucket=text(q,"bucket"),key=text(q,"key");
        if(mode.equals("versioned_overlap"))return versionedOverlap(original,bucket,key);
        if(mode.equals("dual_conditional_write"))return dualConditional(original,bucket,key,size);
        if(mode.equals("bucket_gone"))return bucketGone(original,bucket,key,size);
        if(mode.equals("multipart_resend")){if(size!=8)throw new IllegalArgumentException("original multipart resend size must be 8 bytes");return multipart(original,bucket,key);}
        if(!mode.equals("read"))return upload(original,bucket,key,size,mode);
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
    static long independentVerify(String bucket,String key,long size,char value) {
        try(S3Identities verifier=new S3Identities()){return verify(verifier.get("main"),bucket,key,size,value,null).count;}
    }
    static Map<String,Object> upload(S3Client original,String bucket,String key,long size,String mode) {
        boolean dual=mode.equals("dual_write"),conditional=mode.equals("conditional_write");
        put(original,bucket,key,dual?0:size,'A');
        if(!dual && !conditional)independentVerify(bucket,key,size,'A');
        final long[] callbacks={0};final RuntimeException[] failure={null};
        Runnable interrupt=()->{
            callbacks[0]++;
            try {if(dual)put(original,bucket,key,size,'A');else independentVerify(bucket,key,size,'A');}
            catch(RuntimeException error){failure[0]=error;throw error;}
        };
        PutObjectRequest.Builder request=PutObjectRequest.builder().bucket(bucket).key(key).contentLength(size);
        if(conditional)request.overrideConfiguration(c->c.putHeader("If-Match","*"));
        // Each new provider stream is an actual SDK pass over the same repeatable
        // logical file. Count/actions are shared across checksum/signing/retry reads;
        // never suppress prehash callbacks or hardcode a last network pass.
        try {original.putObject(request.build(),RequestBody.fromContentProvider(()->new ConstantInput(size,'B',interrupt),size,"application/octet-stream"));}
        catch(RuntimeException error){if(failure[0]!=null)throw failure[0];throw error;}
        long finalSize=independentVerify(bucket,key,size,'B');
        Map<String,Object> out=new LinkedHashMap<>();out.put("mode",mode);out.put("eof_callbacks",callbacks[0]);out.put("final_size",finalSize);return out;
    }

    static PutObjectResponse callbackPut(S3Client client,String bucket,String key,long size,char value,String condition,Runnable action,long[] callbacks,RuntimeException[] failure){
        PutObjectRequest.Builder request=PutObjectRequest.builder().bucket(bucket).key(key).contentLength(size);
        if(condition!=null)request.overrideConfiguration(c->c.putHeader("If-Match",condition));
        Runnable callback=()->{callbacks[0]++;try{action.run();}catch(RuntimeException error){failure[0]=error;throw error;}};
        try{return client.putObject(request.build(),RequestBody.fromContentProvider(()->new ConstantInput(size,value,callback),size,"application/octet-stream"));}
        catch(RuntimeException error){if(failure[0]!=null)throw failure[0];throw error;}
    }
    static void sourceError(S3Exception error,int status,String code){
        if(error.statusCode()!=status || error.awsErrorDetails()==null || !code.equals(error.awsErrorDetails().errorCode()))throw error;
    }
    static Map<String,Object> dualConditional(S3Client client,String bucket,String key,long size){
        PutObjectResponse first=put(client,bucket,key,size,'A');independentVerify(bucket,key,size,'A');
        if(first.eTag()==null)throw new IllegalStateException("actual initial A ETag is missing");
        String match=first.eTag().replace(Character.toString((char)34),"");long[] callbacks={0};RuntimeException[] failure={null};
        Runnable nested=()->client.putObject(PutObjectRequest.builder().bucket(bucket).key(key).contentLength(size).overrideConfiguration(c->c.putHeader("If-Match",match)).build(),RequestBody.fromContentProvider(()->new ConstantInput(size,'B',null),size,"application/octet-stream"));
        S3Exception observed=null;
        try{callbackPut(client,bucket,key,size,'C',match,nested,callbacks,failure);}
        catch(S3Exception error){sourceError(error,412,"PreconditionFailed");observed=error;}
        if(observed==null)throw new IllegalStateException("source expected actual 412/PreconditionFailed, outer C upload succeeded");
        long finalSize=independentVerify(bucket,key,size,'B');
        Map<String,Object> out=new LinkedHashMap<>();out.put("mode","dual_conditional_write");out.put("eof_callbacks",callbacks[0]);out.put("observed_error_status",observed.statusCode());out.put("observed_error_code",observed.awsErrorDetails().errorCode());out.put("error_origin",failure[0]==null?"outer_put":"nested_callback");out.put("final_size",finalSize);return out;
    }
    static Map<String,Object> bucketGone(S3Client client,String bucket,String key,long size){
        long[] callbacks={0};RuntimeException[] failure={null};S3Exception observed=null;
        try{callbackPut(client,bucket,key,size,'A',null,()->client.deleteBucket(DeleteBucketRequest.builder().bucket(bucket).build()),callbacks,failure);}
        catch(S3Exception error){sourceError(error,404,"NoSuchBucket");observed=error;}
        if(observed==null)throw new IllegalStateException("source expected actual 404/NoSuchBucket, upload succeeded");
        Map<String,Object> out=new LinkedHashMap<>();out.put("mode","bucket_gone");out.put("eof_callbacks",callbacks[0]);out.put("observed_error_status",observed.statusCode());out.put("observed_error_code",observed.awsErrorDetails().errorCode());out.put("error_origin",failure[0]==null?"outer_put":"nested_callback");return out;
    }

    static String part(S3Client client,String bucket,String key,String upload,char value,long[] eof,Runnable action) {
        RuntimeException[] failure={null};
        Runnable callback=()->{eof[0]++;if(action!=null)try{action.run();}catch(RuntimeException error){failure[0]=error;throw error;}};
        try {
            UploadPartResponse result=client.uploadPart(UploadPartRequest.builder().bucket(bucket).key(key).uploadId(upload).partNumber(1).contentLength(8L).build(),RequestBody.fromContentProvider(()->new ConstantInput(8,value,callback),8,"application/octet-stream"));
            if(result.eTag()==null)throw new IllegalStateException("actual UploadPart ETag is missing");
            return result.eTag();
        } catch(RuntimeException error){if(failure[0]!=null)throw failure[0];throw error;}
    }
    static String stripQuotes(String value) {
        int start=0,end=value.length();while(start<end && value.charAt(start)=='"')start++;while(end>start && value.charAt(end-1)=='"')end--;return value.substring(start,end);
    }
    static void complete(S3Client client,String bucket,String key,String upload,List<CompletedPart> parts){
        client.completeMultipartUpload(CompleteMultipartUploadRequest.builder().bucket(bucket).key(key).uploadId(upload).multipartUpload(CompletedMultipartUpload.builder().parts(parts).build()).build());
    }
    static String create(S3Client client,String bucket,String key,List<String> pending){
        String upload=client.createMultipartUpload(CreateMultipartUploadRequest.builder().bucket(bucket).key(key).build()).uploadId();
        if(upload==null || upload.isEmpty())throw new IllegalStateException("actual CreateMultipartUpload UploadId is missing");pending.add(upload);return upload;
    }
    static Map<String,Object> multipart(S3Client client,String bucket,String key){
        List<String> pending=new ArrayList<>();RuntimeException primary=null;
        try {
            String dry=create(client,bucket,key,pending);long[] dryEof={0};
            String dryTag=part(client,bucket,key,dry,'C',dryEof,null);
            complete(client,bucket,key,dry,Arrays.asList(CompletedPart.builder().partNumber(1).eTag(stripQuotes(dryTag)).build()));pending.remove(dry);
            client.deleteObject(DeleteObjectRequest.builder().bucket(bucket).key(key).build());
            String actual=create(client,bucket,key,pending);long[] outerEof={0},nestedEof={0},triggered={0};List<CompletedPart> parts=new ArrayList<>();
            Runnable action=()->{
                if(outerEof[0]==dryEof[0]){
                    String nested=part(client,bucket,key,actual,'B',nestedEof,null);parts.add(CompletedPart.builder().partNumber(1).eTag(stripQuotes(nested)).build());triggered[0]++;
                }
            };
            String outer=part(client,bucket,key,actual,'A',outerEof,action);parts.add(CompletedPart.builder().partNumber(1).eTag(stripQuotes(outer)).build());
            complete(client,bucket,key,actual,parts);pending.remove(actual);
            long finalSize=independentVerify(bucket,key,8,'A');
            Map<String,Object> out=new LinkedHashMap<>();out.put("mode","multipart_resend");out.put("dry_eof_reads",dryEof[0]);out.put("outer_eof_reads",outerEof[0]);out.put("nested_eof_reads",nestedEof[0]);out.put("nested_actions",triggered[0]);out.put("completed_entries",parts.size());out.put("final_size",finalSize);return out;
        }catch(RuntimeException error){primary=error;throw error;}
        finally {
            RuntimeException cleanup=null;
            for(String upload:pending)try{client.abortMultipartUpload(AbortMultipartUploadRequest.builder().bucket(bucket).key(key).uploadId(upload).build());}
            catch(S3Exception error){if(error.statusCode()!=404 && cleanup==null)cleanup=error;}
            catch(RuntimeException error){if(cleanup==null)cleanup=error;}
            if(cleanup!=null){if(primary!=null)primary.addSuppressed(cleanup);else throw cleanup;}
        }
    }

    static void start(List<Thread> threads,Runnable action,List<Map<String,Object>> failures){
        Thread thread=new Thread(()->{try{action.run();}catch(Throwable error){
            Map<String,Object> row=new LinkedHashMap<>();row.put("error",error.toString());
            if(error instanceof S3Exception){S3Exception sdk=(S3Exception)error;row.put("status_code",sdk.statusCode());if(sdk.awsErrorDetails()!=null)row.put("error_code",sdk.awsErrorDetails().errorCode());}
            failures.add(row);
        }},"osv-original-version-worker");
        threads.add(thread);thread.start();
    }
    static void joinAll(List<Thread> threads){
        boolean interrupted=false;
        for(Thread thread:threads)while(thread.isAlive())try{thread.join();}catch(InterruptedException error){interrupted=true;}
        if(interrupted)Thread.currentThread().interrupt();
    }
    static int startDeletes(S3Client client,String bucket,List<Thread> threads,List<Map<String,Object>> failures){
        ListObjectVersionsResponse listed=client.listObjectVersions(ListObjectVersionsRequest.builder().bucket(bucket).build());
        for(ObjectVersion version:listed.versions()){
            if(version.key()==null || version.versionId()==null)throw new IllegalStateException("actual listed version is missing Key/VersionId; refusing unversioned delete fallback");
            start(threads,()->client.deleteObject(DeleteObjectRequest.builder().bucket(bucket).key(version.key()).versionId(version.versionId()).build()),failures);
        }
        return listed.versions().size();
    }
    static Map<String,Object> versionedOverlap(S3Client client,String bucket,String key){
        List<Thread> all=new ArrayList<>();List<Map<String,Object>> failures=Collections.synchronizedList(new ArrayList<>());List<Integer> groups=new ArrayList<>();
        try {
            for(int round=0;round<3;round++){
                for(int i=0;i<3;i++){final int index=i;start(all,()->client.putObject(PutObjectRequest.builder().bucket(bucket).key(key).build(),RequestBody.fromBytes(("data "+index).getBytes(StandardCharsets.UTF_8))),failures);}
                groups.add(startDeletes(client,bucket,all,failures));
            }
            joinAll(all);if(Thread.currentThread().isInterrupted())throw new IllegalStateException("version coordinator interrupted after joining actual workers");
            groups.add(startDeletes(client,bucket,all,failures));joinAll(all);
            ListObjectVersionsResponse finalList=client.listObjectVersions(ListObjectVersionsRequest.builder().bucket(bucket).build());
            if(finalList.hasVersions())throw new IllegalStateException("source expected Versions absent after overlapping create/remove and final cleanup");
            Map<String,Object> out=new LinkedHashMap<>();out.put("mode","versioned_overlap");out.put("source_put_threads",9);out.put("actual_delete_group_sizes",groups);out.put("thread_errors",new ArrayList<>(failures));out.put("versions_present",finalList.hasVersions());return out;
        }finally{joinAll(all);}
    }

    static String text(JsonNode q,String name){if(!q.has(name)||!q.get(name).isTextual()||q.get(name).textValue().isEmpty())throw new IllegalArgumentException(name+" must be a nonempty string");return q.get(name).textValue();}
}
