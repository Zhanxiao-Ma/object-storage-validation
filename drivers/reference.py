#!/usr/bin/env python3
"""Local filesystem protocol reference, exclusively for harness self-tests.

This is not an S3 emulator or cloud compatibility result. Data is actually written
and read; multipart state lives separately until the completion operation commits it.
"""
import hashlib
from email.utils import formatdate
from urllib.parse import quote
import json
import os
import re
from pathlib import Path
import sys
import time
import uuid

root = Path(os.environ["OSV_REFERENCE_ROOT"]).resolve()
root.mkdir(parents=True, exist_ok=True)
base_root=root


class OperationError(ValueError):
    """Represent a rejected local object operation with an explicit semantic code, not an SDK claim."""
    def __init__(self,code,status=None):
        super().__init__(code)
        self.code=code
        self.status=status


def path(key):
    # Object keys are opaque strings, not host filesystem paths (including trailing slash/control characters).
    candidate = root/".objects"/hashlib.sha256(key.encode()).hexdigest()
    return candidate


def remember(key,metadata=None,content_type="application/octet-stream"):
    file=path(key)
    file.with_suffix(".json").write_text(json.dumps({"key":key,"metadata":metadata or {},"content_type":content_type}))


def inventory(prefix):
    objects=[]
    for metadata in (root/".objects").glob("*.json"):
        key=json.loads(metadata.read_text())["key"]
        if not key.startswith(prefix): continue
        file=path(key)
        stat=file.stat()
        objects.append({"key":key,"size":stat.st_size,"last_modified_ms":int(stat.st_mtime*1000),
                        "etag":hashlib.md5(file.read_bytes()).hexdigest()})
    return sorted(objects,key=lambda item:item["key"])


def execute(q):
    global root
    root=base_root if "bucket" not in q else base_root/".buckets"/hashlib.sha256(q["bucket"].encode()).hexdigest()
    op = q["op"]
    if op == "info":
        return {"language": "python", "api": "local", "backend": "local-reference", "sdk_version": "1", "protocol_version": 1,
                "capabilities": ["put", "get", "head", "list", "delete", "mp_create", "mp_part", "mp_complete", "mp_abort", "bucket_create", "list_page", "get_conditional", "put_metadata", "get_metadata", "head_metadata", "copy", "copy_replace", "get_http", "tags_put", "tags_get", "tags_delete", "put_http", "head_http", "bucket_create_raw", "bucket_head", "bucket_delete", "bucket_location", "list_v1_page", "list_v2_observed"]}
    if op=="bucket_create_raw":
        if "bucket" not in q:raise ValueError("raw bucket creation requires explicit bucket")
        if root.exists():return {"status_code":200}
        root.mkdir(parents=True)
        (root/".bucket.json").write_text(json.dumps({"name":q["bucket"],"location":q.get("location")}))
        return {"status_code":200}
    if op in ("bucket_head","bucket_delete","bucket_location") or "bucket" in q:
        if not root.exists():raise OperationError("NoSuchBucket",404)
    if op=="bucket_head":return {"status_code":200}
    if op=="bucket_location":return {"location":json.loads((root/".bucket.json").read_text()).get("location"),"status_code":200}
    if op=="bucket_delete":
        if "bucket" not in q:raise ValueError("raw bucket deletion requires explicit bucket")
        if any(file.is_file() and file.name!=".bucket.json" for file in root.rglob("*")):raise OperationError("BucketNotEmpty",409)
        for folder in sorted((file for file in root.rglob("*") if file.is_dir()),key=lambda file:len(file.parts),reverse=True):folder.rmdir()
        (root/".bucket.json").unlink()
        root.rmdir();return {"status_code":204}
    if op in ("list_v1_page","list_v2_observed"):
        options={**q,"op":"list_page"}
        if "start_after" in q:options["marker"]=q["start_after"]
        page=execute(options);v2=op=="list_v2_observed"
        fields=["Name","Prefix","MaxKeys","IsTruncated"]
        if q.get("delimiter"):fields.append("Delimiter")
        if page["objects"]:fields.append("Contents")
        if page["common_prefixes"]:fields.append("CommonPrefixes")
        for item in page["objects"]:
            item["etag"]=chr(34)+item["etag"]+chr(34)
            item["storage_class"]="STANDARD"
            if not v2 or q.get("fetch_owner",False):item["owner"]={"id":str(path(item["key"]).stat().st_uid)}
        page.update(status_code=200,http_headers={},fields=fields,delimiter=q.get("delimiter") or None,field_presence_source="local_model")
        if v2:
            page["key_count"]=len(page["keys"])+len(page["common_prefixes"]);fields.append("KeyCount")
            page["token"]=q.get("token");page["start_after"]=q.get("start_after")
            if "token" in q:fields.append("ContinuationToken")
            if "start_after" in q:fields.append("StartAfter")
            if page["truncated"]:fields.append("NextContinuationToken")
            else:page["next_token"]=None
        else:
            page["marker"]=q.get("marker","");fields.append("Marker")
            page["next_marker"]=page["next_token"] if page["truncated"] and q.get("delimiter") else None
            if page["next_marker"] is not None:fields.append("NextMarker")
            page.pop("next_token",None)
        if q.get("encoding_type")=="url":
            fields.append("EncodingType");page["encoding_type"]="url"
            for name in ("keys","common_prefixes"):page[name]=[quote(value,safe="/") for value in page[name]]
            for item in page["objects"]:item["key"]=quote(item["key"],safe="/")
            for name in ("prefix","delimiter","marker","next_marker","start_after"):
                if page.get(name) is not None:page[name]=quote(page[name],safe="/")
        return page
    key = path(q.get("key", "unused"))
    if op == "bucket_create":
        root.mkdir(exist_ok=True,parents=True)
    elif op == "list_page":
        objects=inventory(q.get("prefix","")); delimiter=q.get("delimiter","")
        entries={}
        for obj in objects:
            suffix=obj["key"][len(q.get("prefix","")):]
            if delimiter and delimiter in suffix:
                entries[q.get("prefix","")+suffix.split(delimiter,1)[0]+delimiter]=None
            else:
                entries[obj["key"]]=obj
        marker=q.get("token",q.get("marker",""))
        names=[name for name in sorted(entries) if name>marker]
        limit=q.get("max_keys",1000)
        selected=names[:limit]
        truncated=limit>0 and len(names)>limit
        objs=[entries[name] for name in selected if entries[name] is not None]
        return {"keys":[obj["key"] for obj in objs],"objects":objs,
                "common_prefixes":[name for name in selected if entries[name] is None],
                "truncated":truncated,"next_token":selected[-1] if truncated else "",
                "prefix":q.get("prefix",""),"delimiter":q.get("delimiter",""),"max_keys":limit}
    elif op in ("put","put_metadata","put_http"):
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_bytes(Path(q["body_path"]).read_bytes())
        remember(q["key"],q.get("metadata",{}),q.get("content_type","application/octet-stream"))
        if op=="put_http":
            info=json.loads(key.with_suffix(".json").read_text())
            info.update({field:q[field] for field in ("cache_control","expires_ms") if field in q})
            key.with_suffix(".json").write_text(json.dumps(info))
            return {"status_code":200,"etag":chr(34)+hashlib.md5(key.read_bytes()).hexdigest()+chr(34),"http_headers":{}}

    elif op in ("get", "get_conditional", "get_metadata", "get_http"):
        content = key.read_bytes()
        etag='"'+hashlib.md5(content).hexdigest()+'"'
        modified=int(key.stat().st_mtime)*1000
        if "if_match" in q and q["if_match"] not in (etag,"*"):raise OperationError("PreconditionFailed")
        if "if_unmodified_since_ms" in q and modified>q["if_unmodified_since_ms"] and "if_match" not in q:raise OperationError("PreconditionFailed")
        if "if_none_match" in q and q["if_none_match"] in (etag,"*"):raise OperationError("NotModified")
        if "if_modified_since_ms" in q and modified<=q["if_modified_since_ms"] and "if_none_match" not in q:raise OperationError("NotModified")
        total=len(content); response_headers={}; status=200
        if "range_header" in q:
            match=re.fullmatch(r"bytes=(\d*)-(\d*)",q["range_header"])
            if not match or not any(match.groups()):raise OperationError("InvalidRange")
            left,right=match.groups()
            start=int(left) if left else max(0,total-int(right))
            end=min(total-1,int(right)) if left and right else total-1
            if start>=total or start>end:raise OperationError("InvalidRange")
            content=content[start:end+1];status=206
            response_headers["content-range"]=f"bytes {start}-{end}/{total}"
        if "offset" in q:
            if q["offset"]>=len(content):raise OperationError("InvalidRange")
            content = content[q["offset"]:q["offset"]+q["length"]]
        Path(q["output_path"]).write_bytes(content)
        if op=="get_http":
            return {"status_code":status,"http_headers":response_headers}
        if op=="get_metadata":
            info=json.loads(key.with_suffix(".json").read_text())
            return {"key":q["key"],"size":len(content),"last_modified_ms":int(key.stat().st_mtime*1000),
                    "etag":etag.strip('"'),"metadata":info["metadata"],"content_type":info["content_type"]}
    elif op=="head_http":
        info=json.loads(key.with_suffix(".json").read_text());headers={}
        if "cache_control" in info:headers["cache-control"]=info["cache_control"]
        if "expires_ms" in info:headers["expires"]=formatdate(info["expires_ms"]/1000,usegmt=True)
        return {"size":key.stat().st_size,"status_code":200,"http_headers":headers}
    elif op in ("head","head_metadata"):
        return {"key":q["key"],"size": key.stat().st_size,"last_modified_ms":int(key.stat().st_mtime*1000),
                "etag":hashlib.md5(key.read_bytes()).hexdigest(),**{k:v for k,v in json.loads(key.with_suffix(".json").read_text()).items() if k in ("metadata","content_type")}}
    elif op in ("tags_put","tags_get","tags_delete"):
        info=json.loads(key.with_suffix(".json").read_text())
        if op=="tags_get":return {"tags":info.get("tags",[]),"status_code":200}
        tags=q["tags"] if op=="tags_put" else []
        if len(tags)>10 or any(len(tag["key"])>128 or len(tag["value"])>256 for tag in tags):raise OperationError("InvalidTag")
        info["tags"]=tags
        key.with_suffix(".json").write_text(json.dumps(info))
        return {"status_code":200 if op=="tags_put" else 204}
    elif op in ("copy","copy_replace"):
        source=path(q["source_key"])
        data=source.read_bytes()
        if q["source_key"]==q["key"] and op=="copy":raise OperationError("InvalidRequest")
        info=json.loads(source.with_suffix(".json").read_text())
        key.parent.mkdir(parents=True,exist_ok=True);key.write_bytes(data)
        remember(q["key"],q["metadata"] if op=="copy_replace" else info["metadata"],
                 q["content_type"] if op=="copy_replace" else info["content_type"])
        return {"etag":hashlib.md5(data).hexdigest()}
    elif op == "delete":
        key.unlink(missing_ok=True)
        key.with_suffix(".json").unlink(missing_ok=True)
    elif op == "list":
        return {"keys":[obj["key"] for obj in inventory(q["prefix"])]}
    elif op == "mp_create":
        ident = uuid.uuid4().hex
        (root/".uploads"/ident).mkdir(parents=True)
        return {"upload_id": ident}
    elif op == "mp_part":
        if not (root/".uploads"/q["upload_id"]).exists():raise OperationError("NoSuchUpload")
        data = Path(q["body_path"]).read_bytes()
        (root/".uploads"/q["upload_id"]/str(q["part_number"])).write_bytes(data)
        return {"etag": hashlib.sha256(data).hexdigest()}
    elif op == "mp_complete":
        parts = root/".uploads"/q["upload_id"]
        if not parts.exists():raise OperationError("NoSuchUpload")
        content = bytearray()
        for index,part in enumerate(q["parts"]):
            file=parts/str(part["part_number"])
            if not file.exists():raise OperationError("InvalidPart")
            data = file.read_bytes()
            if hashlib.sha256(data).hexdigest() != part["etag"]:
                raise OperationError("InvalidPart")
            if index<len(q["parts"])-1 and len(data)<int(os.environ.get("OSV_REFERENCE_MIN_PART_SIZE",str(5*1024*1024))):
                raise OperationError("EntityTooSmall")
            content.extend(data)
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_bytes(content)
        remember(q["key"])
        for part in parts.iterdir():
            part.unlink()
        parts.rmdir()
    elif op == "mp_abort":
        parts = root/".uploads"/q["upload_id"]
        if parts.exists():
            for part in parts.iterdir():
                part.unlink()
            parts.rmdir()
    else:
        raise ValueError("unsupported operation")
    return {}


for line in sys.stdin:
    start = time.monotonic_ns()
    try:
        result = {**execute(json.loads(line)), "ok": True}
    except OperationError as error:
        result={"ok":False,"error_kind":"not_found" if error.status==404 else "local_error","error_code":error.code,"error":str(error)}
        if error.status is not None:result["status_code"]=error.status
    except FileNotFoundError:
        result = {"ok": False, "error_kind": "not_found", "error_code":"NoSuchKey", "status_code":404, "error": "file missing"}
    except Exception as error:
        result = {"ok": False, "error_kind": "local_error", "error": str(error)}
    result["elapsed_ns"] = time.monotonic_ns()-start
    print(json.dumps(result), flush=True)
