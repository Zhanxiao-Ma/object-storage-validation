"""Clean only actual historical versions and delete markers inside freshly owned test buckets."""
from .bucket_scope import BucketScope,SCOPE_OPERATIONS

VERSION_SCOPE_OPERATIONS=SCOPE_OPERATIONS+" versions_list delete_version"

class VersionScope(BucketScope):
    """Prevent historical objects surviving current-key cleanup: retain producer IDs and purge actual version pages before verifying bucket deletion."""
    def __init__(self,probe,name=None):
        super().__init__(probe,name)
        self.version_keys={}

    def raw(self,op,**options):
        result=super().raw(op,**options)
        if result["ok"] and "key" in options and "version_id" in result:
            self.version_keys.setdefault(result["version_id"],set()).add(options["key"])
        return result

    def cleanup_objects(self):
        failures=[];previous=None
        # Restart the first page after deleting its real versions; do not depend on
        # a deleted cursor or issue unversioned DELETEs that create new markers.
        for page_number in range(10000):
            result=self.raw("versions_list",max_keys=1000)
            if not result["ok"]:return failures+[result]
            rows=result["versions"]+result["delete_markers"]
            if not rows:
                if result.get("truncated") is not False:raise RuntimeError("empty version cleanup page did not confirm completion")
                return failures
            signature=tuple(sorted((row["key"],row["version_id"]) for row in rows))
            if signature==previous:raise RuntimeError("version cleanup did not advance: "+str(failures))
            previous=signature
            for row in rows:
                key,ident=row["key"],row["version_id"]
                # Use original producer keys only for cleanup when a typed XML
                # getter loses whitespace. The case still receives original SDK values.
                candidates={original for original in self.version_keys.get(ident,set()) if original.strip()==key.strip()}
                if not candidates:candidates={original for original in self.written_keys if original.strip()==key.strip()}
                for original in sorted(candidates or {key}):
                    deleted=self.raw("delete_version",key=original,version_id=ident)
                    if not deleted["ok"]:failures.append(deleted)
        raise RuntimeError("version cleanup exceeded page safety bound")
