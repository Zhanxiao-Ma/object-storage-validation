package io.github.storagevalidation;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.util.*;

/** Exposes complete file operations without control-process protocol-step scheduling.
 * Prepared parts are uploaded by the selected SDK and aborted after failure; bulk
 * deletion is split at the S3 request limit and every returned error is retained.
 */
public final class FileOperations {
    private static final ObjectMapper JSON=new ObjectMapper();
    /** Invokes the selected SDK adapter so file operations use its real requests. */
    public interface Call { Map<String,Object> execute(JsonNode q) throws Exception; }
    public static Map<String,Object> execute(JsonNode q, Call call) throws Exception {
        if(q.path("op").asText().equals("delete_files")) {
            List<Object> errors=new ArrayList<>();
            JsonNode objects=q.path("objects");
            if(!objects.isArray() || objects.size()==0)throw new IllegalArgumentException("delete_files requires objects");
            for(int start=0;start<objects.size();start+=1000) {
                ObjectNode request=JSON.createObjectNode().put("op","delete_many").put("quiet",true);
                var items=request.putArray("objects");
                for(int i=start;i<Math.min(start+1000,objects.size());i++)items.add(objects.get(i));
                Map<String,Object> response=call.execute(request);
                Object returned=response.get("errors");
                if(returned instanceof Collection<?>)errors.addAll((Collection<?>)returned);
            }
            return Map.of("errors",errors);
        }
        JsonNode paths=q.path("part_paths");
        if(!paths.isArray() || paths.size()==0 || paths.size()>10000)throw new IllegalArgumentException("multipart_upload requires 1..10000 prepared part paths");
        String key=q.path("key").asText();
        Object upload=call.execute(JSON.createObjectNode().put("op","mp_create").put("key",key)).get("upload_id");
        if(upload==null)throw new IllegalStateException("missing upload_id");
        try {
            List<Map<String,Object>> parts=new ArrayList<>();
            for(JsonNode path:paths) {
                int number=parts.size()+1;
                Map<String,Object> result=call.execute(JSON.createObjectNode().put("op","mp_part").put("key",key)
                    .put("upload_id",upload.toString()).put("part_number",number).put("body_path",path.asText()));
                parts.add(Map.of("part_number",number,"etag",result.get("etag")));
            }
            ObjectNode request=JSON.createObjectNode().put("op","mp_complete").put("key",key).put("upload_id",upload.toString());
            request.set("parts",JSON.valueToTree(parts));
            return call.execute(request);
        } catch(Exception error) {
            try {call.execute(JSON.createObjectNode().put("op","mp_abort").put("key",key).put("upload_id",upload.toString()));}
            catch(Exception cleanup){throw new RuntimeException(error.getMessage()+"; abort failed: "+cleanup.getMessage(),error);}
            throw error;
        }
    }
}
