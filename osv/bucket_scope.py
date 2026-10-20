"""Own uniquely named test buckets so bucket-wide scenarios can clean their real data on every assertion path."""
import re
import uuid
from .scenarios import require_ok

SCOPE_OPERATIONS="bucket_head bucket_create_raw bucket_delete list delete"

class BucketScope:
    """Prevent bucket-wide tests from sharing production state: verify a fresh generated name, create it and clean only that owned bucket."""
    def __init__(self,probe,name=None):
        self.probe=probe
        self.name="osv-"+uuid.uuid4().hex if name is None else name
        if not isinstance(self.name,str) or not re.fullmatch(r"osv-[0-9a-f]{32}",self.name):raise ValueError("isolated bucket must use the generated osv UUID namespace")
        self.created=False
        self.cleanup_completed=False

    def raw(self,op,**options):
        if "bucket" in options:raise ValueError("cannot override the owned bucket scope")
        return self.probe.ctx.worker.call({"op":op,"bucket":self.name,**options})

    def call(self,op,**options):
        return require_ok(self.raw(op,**options))

    def put(self,key,data):
        self.probe.ctx.body.write_bytes(data)
        return self.call("put",key=key,body_path=str(self.probe.ctx.body))

    def __enter__(self):
        existing=self.raw("bucket_head")
        if existing["ok"]:raise RuntimeError("generated test bucket already exists; refusing reuse: "+self.name)
        if (existing.get("error_kind"),existing.get("status_code"))!=("not_found",404):raise RuntimeError("cannot establish test bucket absence: "+str(existing))
        region=self.probe.ctx.worker.info.get("region","us-east-1")
        options={"location":region} if region and region!="us-east-1" else {}
        response=self.raw("bucket_create_raw",**options)
        if not response["ok"]:
            raise RuntimeError("test bucket creation failed; verify ambiguous transport outcomes before manual cleanup of "+self.name+": "+str(response))
        self.created=True
        return self

    def __exit__(self,error_type,error,traceback):
        if not self.created:return False
        try:
            exists=self.raw("bucket_head")
            if exists["ok"]:
                for key in self.call("list",prefix="")["keys"]:self.call("delete",key=key)
                self.call("bucket_delete")
                gone=self.raw("bucket_head")
                if gone["ok"] or (gone.get("error_kind"),gone.get("status_code"))!=("not_found",404):raise RuntimeError("bucket absence not confirmed after deletion: "+str(gone))
            elif (exists.get("error_kind"),exists.get("status_code"))!=("not_found",404):raise RuntimeError(str(exists))
        except Exception as cleanup_error:
            raise RuntimeError((str(error)+"; " if error else "")+"isolated bucket cleanup failed for "+self.name+": "+str(cleanup_error)) from cleanup_error
        self.cleanup_completed=True
        return False
