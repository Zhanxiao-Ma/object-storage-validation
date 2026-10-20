"""Independent, language-neutral object semantics inspired by objbench and s3-tests.

Only scenario ideas are referenced; no upstream runner or source is required.
Each case provisions its own data and uses real worker operations for read-back.
"""
from dataclasses import dataclass, replace
import random

from .scenarios import Profile, Suite, assert_equal, require_ok

JUICE = "https://github.com/juicedata/juicefs/blob/29c60bbbeea410c6c0cb7a49540044e0c099dffb/cmd/objbench.go"
CEPH = "https://github.com/ceph/s3-tests/blob/5b3987116eca0b4b8fa57d6b10f3db57bab196b6/s3tests/functional/test_s3.py"
CATALOG_VERSION = 15


@dataclass(frozen=True)
class Case:
    """Keep an executable check and its declared oracle/source together for auditable coverage."""
    name: str
    purpose: str
    expected: str
    operations: frozenset
    source: str
    reference: str
    execute: object
    observation: bool = False

    def metadata(self):
        return {"case": self.name, "purpose": self.purpose, "expected": self.expected,
                "operations": sorted(self.operations), "source": self.source,
                "reference": self.reference, "gate": not self.observation}


class Probe:
    """Scope real worker IO to one case so checks never depend on another case running first."""
    def __init__(self, ctx):
        self.ctx = ctx

    def call(self, op, name="object", **args):
        return require_ok(self.raw(op, name, **args))

    def raw(self, op, name="object", **args):
        return self.ctx.worker.call({"op": op, "key": self.ctx.prefix+name, **args})

    def put(self, data, name="object"):
        self.ctx.body.write_bytes(data)
        self.call("put", name, body_path=str(self.ctx.body))

    def read(self, name="object", **args):
        self.ctx.output.unlink(missing_ok=True)
        self.call("get", name, output_path=str(self.ctx.output), **args)
        return self.ctx.output.read_bytes()

    def missing(self, op, name="object"):
        response = self.raw(op, name, output_path=str(self.ctx.output))
        assert_equal(response["ok"], False)
        assert_equal(response.get("error_kind"), "not_found")

    def keys(self, prefix=""):
        return self.call("list", prefix=self.ctx.prefix+prefix)["keys"]

    def multipart(self, mode):
        """Exercise real upload IDs and returned ETags; derive expected bytes from independent inputs."""
        if mode in ("overwrite", "abort_preserves"):
            self.put(b"previous-committed-value")
        upload = self.call("mp_create")["upload_id"]
        completed = False
        try:
            first = random.Random(2026).randbytes(self.ctx.workload["part_size"])
            tail = b"independent-last-part"
            parts = {}
            def part(number, data):
                self.ctx.body.write_bytes(data)
                result = self.call("mp_part", upload_id=upload, part_number=number,
                                   body_path=str(self.ctx.body))
                parts[number] = {"part_number": number, "etag": result["etag"]}
            if mode == "out_of_order":
                part(2, tail)
                part(1, first)
            else:
                part(1, first)
                if mode == "resend":
                    first = random.Random(2027).randbytes(len(first))
                    part(1, first)
                if mode != "single":
                    part(2, tail)
            if mode in ("abort", "abort_preserves"):
                self.call("mp_abort", upload_id=upload)
                if mode == "abort":
                    self.missing("head")
                    assert_equal(self.keys(), [])
                else:
                    assert_equal(self.read(), b"previous-committed-value")
                return
            if mode == "invisible":
                self.missing("head")
                assert_equal(self.keys(), [])
            request_parts = [parts[n] for n in sorted(parts)]
            if mode == "bad_etag":
                request_parts[0] = {**request_parts[0], "etag": "00000000000000000000000000000000"}
                result = self.raw("mp_complete", upload_id=upload, parts=request_parts)
                assert_equal(result["ok"], False)
                assert_equal(result.get("error_kind"), "local_error" if self.ctx.worker.info.get("api") == "local" else "sdk_error")
                if self.ctx.worker.info.get("api") != "local": assert_equal(result.get("error_code"),"InvalidPart")
                self.missing("head")
                return
            self.call("mp_complete", upload_id=upload, parts=request_parts)
            completed = True
            expected = first if mode == "single" else first+tail
            assert_equal(self.read(), expected)
            assert_equal(self.call("head")["size"], len(expected))
        finally:
            if not completed:
                # Abort is best effort here; explicit abort cases assert success above.
                self.raw("mp_abort", upload_id=upload)


class CaseNotRun(RuntimeError):
    """Keep unmet source prerequisites visible and blocking instead of manufacturing substitute test inputs."""
    def __init__(self,message,*requirements):
        if not isinstance(message,str) or not message or not requirements or any(not isinstance(value,str) or not value for value in requirements):
            raise ValueError("CaseNotRun requires a message and explicit missing requirements")
        super().__init__(message)
        self.requirements=list(requirements)


def run_cases(ctx, cases):
    results = []
    for case in cases:
        missing=case.operations-ctx.worker.capabilities
        if missing:
            results.append({"case":case.name,"status":"not_run","gate":not case.observation,
                            "reason":"unsupported_operations","missing_operations":sorted(missing),
                            "error":"worker does not advertise operations: "+", ".join(sorted(missing))})
            continue
        prefix = ctx.prefix+case.name+"/"
        scoped = replace(ctx, prefix=prefix, body=ctx.directory/(case.name+".body"),
                         output=ctx.directory/(case.name+".output"))
        try:
            behavior = case.execute(Probe(scoped))
            result = {"case": case.name, "status": "observation" if case.observation else "pass"}
            if behavior is not None:
                result["behavior"] = str(behavior)
                if isinstance(behavior,dict):result["evidence"]=behavior
        except CaseNotRun as error:
            result={"case":case.name,"status":"not_run","reason":"missing_prerequisites","missing_requirements":error.requirements,"error":str(error)}
        except Exception as error:
            result = {"case": case.name, "status": "fail", "error": str(error)}
        result["gate"]=not case.observation
        results.append(result)
    return results


def roundtrip(p, data=None, name="object"):
    data = p.ctx.payload if data is None else data
    p.put(data, name)
    assert_equal(p.call("head", name)["size"], len(data))
    assert_equal(p.read(name), data)


def ranged(p, offset, length):
    p.put(p.ctx.payload)
    assert_equal(p.read(offset=offset, length=length), p.ctx.payload[offset:offset+length])


def delete_check(p, existing):
    if existing:
        p.put(p.ctx.payload)
    p.call("delete")
    p.call("delete")
    p.missing("head")
    p.missing("get")
    assert_equal(p.keys(), [])


def list_check(p, count=3):
    names = [f"selected/item-{n:04d}" for n in range(count)]
    for name in names+["selected-neighbor/item", "unrelated/item"]:
        p.put(name.encode(), name)
    expected = {p.ctx.prefix+name for name in names}
    actual = p.keys("selected/")
    assert_equal(set(actual), expected)
    assert_equal(len(actual), len(expected))
    assert_equal(p.keys("missing/"), [])


def overwrite(p, replacement):
    p.put(b"old-data-with-different-length")
    p.put(replacement)
    assert_equal(p.read(), replacement)
    assert_equal(p.call("head")["size"], len(replacement))


def delete_recreate(p):
    p.put(b"old")
    p.call("delete")
    p.missing("get")
    roundtrip(p, b"new-after-deletion")


def observed_range(p, mode):
    data = b"" if mode == "empty" else p.ctx.payload
    p.put(data)
    offset = len(data) if mode in ("empty", "start_overflow") else len(data)-1
    result = p.raw("get", offset=offset, length=2, output_path=str(p.ctx.output))
    expectation="successful read with clipped bytes" if mode=="end_overflow" else "successful read with empty bytes"
    evidence={"source_expected":expectation,"source_severity":"warning" if mode!="empty" else "record-only",
              "source_matches":False,"observed":{"ok":result["ok"]}}
    if not result["ok"]:
        evidence["observed"].update({key:result[key] for key in ("error_kind","error_code","status_code","error") if key in result})
        return evidence
    actual = p.ctx.output.read_bytes()
    assert_equal(actual, data[offset:offset+2])
    evidence["source_matches"]=True; evidence["observed"]["bytes"]=len(actual)
    return evidence


def case(name, purpose, expected, ops, execute, reference, source=JUICE, observation=False):
    return Case(name, purpose, expected, frozenset(ops.split()), source, reference, execute, observation)


CORE = (
    case("object_roundtrip", "普通对象读写与元数据", "读取内容完全一致且 HEAD 大小正确", "put get head", roundtrip, "put/get/head an object"),
    case("empty_roundtrip", "空对象完整生命周期", "空内容且 HEAD 大小为零", "put get head", lambda p: roundtrip(p, b""), "put an empty object"),
    case("missing_get", "读取不存在对象", "失败且归一化为 not_found", "get", lambda p: p.missing("get"), "get non-exist"),
    case("missing_head", "查询不存在对象", "失败且归一化为 not_found", "head", lambda p: p.missing("head"), "head an object"),
    case("range_first", "读取首字节", "等于独立输入的首字节", "put get", lambda p: ranged(p, 0, 1), "get partial object"),
    case("range_last", "读取末字节", "等于独立输入的末字节", "put get", lambda p: ranged(p, len(p.ctx.payload)-1, 1), "get partial object"),
    case("range_middle", "读取中间片段", "等于输入对应切片", "put get", lambda p: ranged(p, len(p.ctx.payload)//3, min(37, len(p.ctx.payload)-len(p.ctx.payload)//3)), "get partial object"),
    case("delete_existing", "删除后不可读取或列举", "重复删除成功且 GET/HEAD 返回 not_found", "put delete head get list", lambda p: delete_check(p, True), "delete an object"),
    case("delete_missing", "删除不存在对象", "重复删除成功", "delete head get list", lambda p: delete_check(p, False), "delete non-exist"),
    case("list_prefix", "列举已写入对象并隔离相邻前缀", "键集合准确、无遗漏或重复", "put list", list_check, "list objects"),
    case("special_key", "特殊键名的签名与编码", "空格、中文、百分号及加号内容与列举一致", "put get head list", lambda p: special_key(p, "中文 space+%25?#&=对象"), "special key"),
    case("multipart_complete", "多分块拼接", "按 part number 拼接且完整读取正确", "mp_create mp_part mp_complete mp_abort get head", lambda p: p.multipart("complete"), "multipart upload"),
    case("multipart_abort", "取消已有分块的上传", "取消后对象不存在且不可列举", "mp_create mp_part mp_abort head list", lambda p: p.multipart("abort"), "multipart upload; abort lifecycle", source=CEPH),
    case("range_end_overflow", "记录 Range 末端超界行为", "记录拒绝或返回正确截断内容；不作为兼容门槛", "put get", lambda p: observed_range(p, "end_overflow"), "get partial object", observation=True),
)


def special_key(p, name):
    roundtrip(p, b"independent-special-key-data", name)
    assert_equal(p.keys(), [p.ctx.prefix+name])


EXTENDED = (
    case("overwrite_shorter", "覆盖写不能保留旧尾部", "读取和 HEAD 均反映新短内容", "put get head", lambda p: overwrite(p, b"new"), "test_object_write_read_update_read_delete", CEPH),
    case("overwrite_longer", "覆盖为更大对象", "完整新内容与大小正确", "put get head", lambda p: overwrite(p, p.ctx.payload+b"expanded"), "test_object_write_read_update_read_delete", CEPH),
    case("overwrite_empty", "覆盖为空对象", "旧内容全部消失且大小为零", "put get head", lambda p: overwrite(p, b""), "test_object_head_zero_bytes; overwrite scenario", CEPH),
    case("delete_recreate", "同一键删除后重建", "不能读取到旧内容", "put get head delete", delete_recreate, "test_object_write_read_update_read_delete", CEPH),
    case("range_full", "Range 覆盖完整对象", "内容与普通 GET 一致", "put get", lambda p: ranged(p, 0, len(p.ctx.payload)), "test_ranged_request_response_code", CEPH),
    case("range_exact_tail", "Range 精确结束于对象末端", "内容长度及尾部字节正确", "put get", lambda p: ranged(p, len(p.ctx.payload)//2, len(p.ctx.payload)-len(p.ctx.payload)//2), "test_ranged_request_return_trailing_bytes_response_code", CEPH),
    case("range_start_overflow", "记录起始位置越界行为", "记录拒绝或空内容；不作为兼容门槛", "put get", lambda p: observed_range(p, "start_overflow"), "test_ranged_request_invalid_range", CEPH, True),
    case("range_empty_object", "记录空对象 Range 行为", "记录拒绝或空内容；不作为兼容门槛", "put get", lambda p: observed_range(p, "empty"), "test_ranged_request_empty_object", CEPH, True),
    case("key_xml", "XML 保留字符键名", "内容与列举原始键名一致", "put get head list", lambda p: special_key(p, "xml-<&>\"\u0027-object"), "test_bucket_listv2_encoding_basic", CEPH),
    case("key_nested", "路径形态对象键", "斜线保留且内容正确", "put get head list", lambda p: special_key(p, "nested/level/object"), "test_bucket_list_prefix_basic", CEPH),
    case("list_many", "跨服务端常见分页阈值列举", "至少 1001 个键全部返回、无重复且前缀隔离", "put list", lambda p: list_check(p, p.ctx.workload["list_objects"]), "test_bucket_listv2_many", CEPH),
    case("multipart_single", "单分块完成", "完成后内容与大小正确", "mp_create mp_part mp_complete mp_abort get head", lambda p: p.multipart("single"), "test_multipart_upload_small", CEPH),
    case("multipart_resend", "相同编号分块重传", "完成对象使用最后一次上传的分块内容", "mp_create mp_part mp_complete mp_abort get head", lambda p: p.multipart("resend"), "test_multipart_upload_resend_part", CEPH),
    case("multipart_out_of_order", "按逆序上传分块", "按编号完成后内容顺序正确", "mp_create mp_part mp_complete mp_abort get head", lambda p: p.multipart("out_of_order"), "test_multipart_upload_contents; ordering scenario", CEPH),
    case("multipart_invisible", "未完成上传不可见", "完成前 HEAD 不存在且 LIST 无对象，完成后内容正确", "mp_create mp_part mp_complete mp_abort get head list", lambda p: p.multipart("invisible"), "test_multipart_upload", CEPH),
    case("multipart_overwrite", "分块完成覆盖已有对象", "GET/HEAD 只反映新对象", "put mp_create mp_part mp_complete mp_abort get head", lambda p: p.multipart("overwrite"), "test_multipart_upload_overwrite_existing_object", CEPH),
    case("multipart_abort_preserves", "取消上传保留已提交旧对象", "取消新分块上传不删除或改变旧对象", "put mp_create mp_part mp_abort get", lambda p: p.multipart("abort_preserves"), "multipart lifecycle; independent extension", CEPH),
    case("multipart_bad_etag", "完成时使用错误 ETag", "完成失败且不能发布对象", "mp_create mp_part mp_complete mp_abort head", lambda p: p.multipart("bad_etag"), "test_multipart_upload_incorrect_etag", CEPH),
)

EXCLUSIONS = (
    "桶命名、账户管理及跨桶权限用例（尚未全量迁移；complete 已增加独立桶生命周期）",
    "文件系统 chmod/chown/mtime（不属于本工具对象接口）",
    "跨桶复制、UploadPartCopy、条件写入及批量删除完整边界（基础批量删除、同桶复制、元数据和条件 GET 已迁移）",
    "版本控制、ACL/Policy、加密、生命周期、对象锁与归档恢复（尚未实现）",
    "LIST 的全部协议版本、参数组合和响应字段（已增加单页参数与元数据；尚未全量迁移）",
    "Range 以外大部分 HTTP 状态/响应头一致性、签名负面用例、故障注入（尚未实现）",
)


def register_suites(registry):
    for name, cases in (("core_objects", CORE), ("extended_objects", EXTENDED)):
        registry.add_suite(Suite(name, frozenset().union(*(c.operations for c in cases)),
            lambda ctx, cases=cases: run_cases(ctx, cases), tuple(c.metadata() for c in cases),case_capabilities=True))
    from .objbench_cases import register
    register(registry)
    from .ceph_objects import register as register_ceph
    register_ceph(registry)
    from .ceph_conditions import register as register_conditions
    register_conditions(registry)
    from .ceph_metadata import register as register_metadata
    register_metadata(registry)
    from .ceph_copy import register as register_copy
    register_copy(registry)
    from .ceph_range import register as register_range
    register_range(registry)
    from .ceph_tags import register as register_tags
    register_tags(registry)
    from .ceph_headers import register as register_headers
    register_headers(registry)
    from .ceph_buckets import register as register_buckets
    register_buckets(registry)
    from .ceph_list_exact import register as register_exact_listing
    register_exact_listing(registry)
    from .ceph_delete_many import register as register_delete_many
    register_delete_many(registry)
    from .ceph_multipart import register as register_multipart
    register_multipart(registry)
    from .ceph_multipart_copy import register as register_multipart_copy
    register_multipart_copy(registry)
    from .ceph_get_part import register as register_get_part
    register_get_part(registry)
    from .ceph_versioning import register as register_versioning
    register_versioning(registry)
    from .ceph_bucket_config import register as register_bucket_config
    register_bucket_config(registry)
    from .ceph_sse_customer import register as register_sse_customer
    register_sse_customer(registry)
    from .ceph_bucket_controls import register as register_bucket_controls
    register_bucket_controls(registry)
    from .ceph_sse_multipart import register as register_sse_multipart
    register_sse_multipart(registry)
    from .ceph_encryption_objects import register as register_encryption_objects
    register_encryption_objects(registry)
    from .ceph_bucket_policy import register as register_bucket_policy
    register_bucket_policy(registry)
    from .ceph_acl import register as register_acl
    register_acl(registry)
    from .ceph_bucket_inventory import register as register_bucket_inventory
    register_bucket_inventory(registry)
    from .ceph_request_headers import register as register_request_headers
    register_request_headers(registry)
    from .ceph_conditional_writes import register as register_conditional_writes
    register_conditional_writes(registry)
    from .ceph_access import register as register_access
    register_access(registry)
    from .ceph_object_interop import register as register_object_interop
    register_object_interop(registry)
    from .ceph_policy_conditions import register as register_policy_conditions
    register_policy_conditions(registry)
    from .ceph_metadata_edges import register as register_metadata_edges
    register_metadata_edges(registry)
    from .ceph_checksum_attributes import register as register_checksum_attributes
    register_checksum_attributes(registry)
    registry.add_profile(Profile("normal", ("core_objects", "objbench_objects"),
        "正常档：对齐锁定 objbench 的对象语义与多对象负载；POSIX 文件系统可选项不适用", EXCLUSIONS))
    registry.add_profile(Profile("complete", ("core_objects", "objbench_objects", "extended_objects", "ceph_object_boundaries", "ceph_conditions", "ceph_metadata", "ceph_copy", "ceph_http_range", "ceph_tags", "ceph_headers", "ceph_buckets", "ceph_exact_listing", "ceph_delete_many", "ceph_exact_multipart", "ceph_multipart_copy", "ceph_get_part", "ceph_versioning", "ceph_bucket_config", "ceph_sse_customer", "ceph_bucket_controls", "ceph_sse_multipart", "ceph_encryption_objects", "ceph_bucket_policy", "ceph_acl", "ceph_bucket_inventory", "ceph_request_headers", "ceph_conditional_writes", "ceph_access", "ceph_object_interop", "ceph_policy_conditions", "ceph_metadata_edges", "ceph_checksum_attributes"),
        "完善档：Ceph 场景迁移中；完整对齐状态与未迁移清单见 alignment.json", EXCLUSIONS))
