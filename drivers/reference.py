#!/usr/bin/env python3
"""Local filesystem protocol reference, exclusively for harness self-tests.

This is not an S3 emulator or cloud compatibility result. Data is actually written
and read; multipart state lives separately until the completion operation commits it.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

root = Path(os.environ["OSV_REFERENCE_ROOT"]).resolve()
root.mkdir(parents=True, exist_ok=True)


def path(key):
    candidate = (root / key).resolve()
    candidate.relative_to(root)
    return candidate


def execute(q):
    op = q["op"]
    if op == "info":
        return {"language": "python", "api": "local", "backend": "local-reference", "sdk_version": "1", "protocol_version": 1,
                "capabilities": ["put", "get", "head", "list", "delete", "mp_create", "mp_part", "mp_complete", "mp_abort"]}
    key = path(q.get("key", "unused"))
    if op == "put":
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_bytes(Path(q["body_path"]).read_bytes())
    elif op == "get":
        content = key.read_bytes()
        if "offset" in q:
            content = content[q["offset"]:q["offset"]+q["length"]]
        Path(q["output_path"]).write_bytes(content)
    elif op == "head":
        return {"size": key.stat().st_size}
    elif op == "delete":
        key.unlink(missing_ok=True)
    elif op == "list":
        return {"keys": sorted(str(p.relative_to(root)) for p in root.rglob("*")
                               if p.is_file() and str(p.relative_to(root)).startswith(q["prefix"]))}
    elif op == "mp_create":
        ident = uuid.uuid4().hex
        (root/".uploads"/ident).mkdir(parents=True)
        return {"upload_id": ident}
    elif op == "mp_part":
        data = Path(q["body_path"]).read_bytes()
        (root/".uploads"/q["upload_id"]/str(q["part_number"])).write_bytes(data)
        return {"etag": hashlib.sha256(data).hexdigest()}
    elif op == "mp_complete":
        parts = root/".uploads"/q["upload_id"]
        content = bytearray()
        for part in q["parts"]:
            data = (parts/str(part["part_number"])).read_bytes()
            if hashlib.sha256(data).hexdigest() != part["etag"]:
                raise ValueError("part checksum mismatch")
            content.extend(data)
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_bytes(content)
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
    except FileNotFoundError:
        result = {"ok": False, "error_kind": "not_found", "error": "file missing"}
    except Exception as error:
        result = {"ok": False, "error_kind": "local_error", "error": str(error)}
    result["elapsed_ns"] = time.monotonic_ns()-start
    print(json.dumps(result), flush=True)
