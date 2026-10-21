"""Independent, language-neutral object semantics inspired by objbench and s3-tests.

Some Ceph modules statically migrate pinned original source bodies with retained
MIT notices; others implement the same inputs and assertions independently.
No external upstream runner or source checkout is required.
Each case provisions its own data and uses real worker operations for read-back.
"""
from dataclasses import dataclass, replace
import random

from .scenarios import Profile, Suite, assert_equal, require_ok

JUICE = "https://github.com/juicedata/juicefs/blob/29c60bbbeea410c6c0cb7a49540044e0c099dffb/cmd/objbench.go"
CEPH = "https://github.com/ceph/s3-tests/blob/5b3987116eca0b4b8fa57d6b10f3db57bab196b6/s3tests/functional/test_s3.py"
CATALOG_VERSION = 29


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
    severity: str = "error"

    def metadata(self):
        return {"case": self.name, "purpose": self.purpose, "expected": self.expected,
                "operations": sorted(self.operations), "source": self.source,
                "reference": self.reference, "severity": self.severity}


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
    """Keep unmet source prerequisites visible instead of manufacturing substitute test inputs."""
    def __init__(self,message,*requirements):
        if not isinstance(message,str) or not message or not requirements or any(not isinstance(value,str) or not value for value in requirements):
            raise ValueError("CaseNotRun requires a message and explicit missing requirements")
        super().__init__(message)
        self.requirements=list(requirements)


def run_cases(ctx, cases):
    results = []
    for case in cases:
        if ctx.selected_cases is not None and case.name not in ctx.selected_cases:
            continue
        missing=case.operations-ctx.worker.capabilities
        if missing:
            results.append({"case":case.name,"status":"not_run","severity":case.severity,
                            "reason":"unsupported_operations","missing_operations":sorted(missing),
                            "error":"worker does not advertise operations: "+", ".join(sorted(missing))})
            continue
        prefix = ctx.prefix+case.name+"/"
        scoped = replace(ctx, prefix=prefix, body=ctx.directory/(case.name+".body"),
                         output=ctx.directory/(case.name+".output"))
        try:
            behavior = case.execute(Probe(scoped))
            result = {"case": case.name, "status": "warning" if isinstance(behavior, dict) and behavior.get("source_matches") is False and case.severity == "warning" else "pass"}
            if behavior is not None:
                result["behavior"] = str(behavior)
                if isinstance(behavior,dict):result["evidence"]=behavior
        except CaseNotRun as error:
            result={"case":case.name,"status":"not_run","reason":"missing_prerequisites","missing_requirements":error.requirements,"error":str(error)}
        except Exception as error:
            result = {"case": case.name, "status": "warning" if case.severity == "warning" else "fail", "error": str(error)}
        result["severity"]=case.severity
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
    evidence={"source_expected":expectation,"source_severity":"warning",
              "source_matches":False,"observed":{"ok":result["ok"]}}
    if not result["ok"]:
        evidence["observed"].update({key:result[key] for key in ("error_kind","error_code","status_code","error") if key in result})
        return evidence
    actual = p.ctx.output.read_bytes()
    assert_equal(actual, data[offset:offset+2])
    evidence["source_matches"]=True; evidence["observed"]["bytes"]=len(actual)
    return evidence


def strict_range(p, empty):
    """Use the existing strict Ceph oracle for duplicate Range boundary coverage."""
    from .ceph_objects import invalid_range
    return invalid_range(p, empty)


def case(name, purpose, expected, ops, execute, reference, source=JUICE, severity="error"):
    return Case(name, purpose, expected, frozenset(ops.split()), source, reference, execute, severity)


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
    case("multipart_complete", "多分块拼接", "按 part number 拼接且完整读取正确", "mp_create mp_part mp_complete mp_abort get head", lambda p: p.multipart("complete"), "multipart upload", severity="warning"),
    case("multipart_abort", "取消已有分块的上传", "取消后对象不存在且不可列举", "mp_create mp_part mp_abort head list", lambda p: p.multipart("abort"), "multipart upload; abort lifecycle", source=CEPH),
    case("range_end_overflow", "记录 Range 末端超界行为", "成功返回末端截断内容；偏离时按 JuiceFS warning 报告", "put get", lambda p: observed_range(p, "end_overflow"), "get partial object", severity="warning"),
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
    case("range_start_overflow", "记录起始位置越界行为", "失败并返回 InvalidRange；可观测 HTTP 状态为 416", "put get", lambda p: strict_range(p, False), "test_ranged_request_invalid_range", CEPH),
    case("range_empty_object", "记录空对象 Range 行为", "失败并返回 InvalidRange；可观测 HTTP 状态为 416", "put get", lambda p: strict_range(p, True), "test_ranged_request_empty_object", CEPH),
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
    "文件系统 chmod/chown/mtime（不属于本工具对象接口）",
    "SigV2 请求头21个源函数及含 SigV2 的日志认证组合1个源函数（尚未迁移）",
    "正常档只选择基础对象/objbench；高级功能迁移与前提条件见完善档逐项目录和alignment.json",
    "迁移记录不代表实云通过；身份、OIDC、KMS、生命周期加速等真实前提缺失时明确not_run",
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
    from .ceph_conditional_versions import register as register_conditional_versions
    register_conditional_versions(registry)
    from .ceph_public_controls import register as register_public_controls
    register_public_controls(registry)
    from .ceph_copy_contracts import register as register_copy_contracts
    register_copy_contracts(registry)
    from .ceph_state_boundaries import register as register_state_boundaries
    register_state_boundaries(registry)
    from .ceph_basic_boundaries import register as register_basic_boundaries
    register_basic_boundaries(registry)
    from .ceph_policy_remaining import register as register_policy_remaining
    register_policy_remaining(registry)
    from .ceph_logging_controls import register as register_logging_controls
    register_logging_controls(registry)
    from .ceph_raw_http import register as register_ceph_raw_http
    register_ceph_raw_http(registry)
    from .ceph_query_edges import register as register_ceph_query_edges
    register_ceph_query_edges(registry)
    from .ceph_lifecycle_expiration import register as register_ceph_lifecycle_expiration
    register_ceph_lifecycle_expiration(registry)
    from .ceph_lifecycle_transition import register as register_ceph_lifecycle_transition
    register_ceph_lifecycle_transition(registry)
    from .ceph_lifecycle_marker_expiration import register as register_ceph_lifecycle_marker_expiration
    register_ceph_lifecycle_marker_expiration(registry)
    from .ceph_object_locks import register as register_ceph_object_locks
    register_ceph_object_locks(registry)
    from .ceph_logging_records import register as register_ceph_logging_records
    register_ceph_logging_records(registry)
    from .ceph_bucket_names import register as register_ceph_bucket_names
    register_ceph_bucket_names(registry)
    from .ceph_cloud_lifecycle import register as register_ceph_cloud_lifecycle
    register_ceph_cloud_lifecycle(registry)
    from .ceph_logging_workloads import register as register_ceph_logging_workloads
    register_ceph_logging_workloads(registry)
    from .ceph_logging_extensions import register as register_ceph_logging_extensions
    register_ceph_logging_extensions(registry)
    from .ceph_metadata_tail import register as register_ceph_metadata_tail
    register_ceph_metadata_tail(registry)
    from .ceph_encrypted_forms import register as register_ceph_encrypted_forms
    register_ceph_encrypted_forms(registry)
    from .ceph_encryption_tail import register as register_ceph_encryption_tail
    register_ceph_encryption_tail(registry)
    from .ceph_logging_policy_transitions import register as register_ceph_logging_policy_transitions
    register_ceph_logging_policy_transitions(registry)
    from .ceph_logging_prefixes import register as register_ceph_logging_prefixes
    register_ceph_logging_prefixes(registry)
    from .ceph_encrypted_copy_matrix import register as register_ceph_encrypted_copy_matrix
    register_ceph_encrypted_copy_matrix(registry)
    from .ceph_logging_tenants import register as register_ceph_logging_tenants
    register_ceph_logging_tenants(registry)
    from .ceph_logging_parallel import register as register_ceph_logging_parallel
    register_ceph_logging_parallel(registry)
    registry.add_profile(Profile("normal", ("core_objects", "objbench_objects"),
        "正常档：对齐锁定 objbench 的对象语义与多对象负载；POSIX 文件系统可选项不适用", EXCLUSIONS))
    from .ceph_presigned_urls import register as register_ceph_presigned_urls
    register_ceph_presigned_urls(registry)
    from .ceph_atomic_streams import register as register_ceph_atomic_streams
    register_ceph_atomic_streams(registry)
    from .ceph_iam_accounts import register as register_ceph_iam_accounts
    register_ceph_iam_accounts(registry)
    from .ceph_iam_inline import register as register_ceph_iam_inline
    register_ceph_iam_inline(registry)
    from .ceph_iam_tenant_actions import register as register_ceph_iam_tenant_actions
    register_ceph_iam_tenant_actions(registry)
    from .ceph_iam_policy_entities import register as register_ceph_iam_policy_entities
    register_ceph_iam_policy_entities(registry)
    from .ceph_logging_remaining import register as register_ceph_logging_remaining
    register_ceph_logging_remaining(registry)
    from .ceph_s3_tail import register as register_ceph_s3_tail
    register_ceph_s3_tail(registry)
    from .ceph_response_bodies import register as register_ceph_response_bodies
    register_ceph_response_bodies(registry)
    from .ceph_s3_root_policy import register as register_ceph_s3_root_policy
    register_ceph_s3_root_policy(registry)
    from .ceph_iam_root_keys import register as register_ceph_iam_root_keys
    register_ceph_iam_root_keys(registry)
    from .ceph_sts_basic import register as register_ceph_sts_basic
    register_ceph_sts_basic(registry)
    from .ceph_iam_role_policies import register as register_ceph_iam_role_policies
    register_ceph_iam_role_policies(registry)
    from .ceph_iam_assumed_roles import register as register_ceph_iam_assumed_roles
    register_ceph_iam_assumed_roles(registry)
    from .ceph_sts_web import register as register_ceph_sts_web
    register_ceph_sts_web(registry)
    from .ceph_iam_oidc import register as register_ceph_iam_oidc
    register_ceph_iam_oidc(registry)
    from .ceph_sts_callers import register as register_ceph_sts_callers
    register_ceph_sts_callers(registry)
    from .ceph_iam_cross_resources import register as register_ceph_iam_cross_resources
    register_ceph_iam_cross_resources(registry)
    from .ceph_select_register import register as register_ceph_select_register
    register_ceph_select_register(registry)
    from .ceph_logging_remaining_extensions import register as register_ceph_logging_remaining_extensions
    register_ceph_logging_remaining_extensions(registry)
    from .ceph_utils import register as register_ceph_utils
    register_ceph_utils(registry)
    from .ceph_logging_remaining_parallel import register as register_remaining_parallel
    register_remaining_parallel(registry)
    from .ceph_auxiliary import register as register_auxiliary
    register_auxiliary(registry)
    from .ceph_logging_remaining_last import register as register_remaining_last
    register_remaining_last(registry)
    registry.add_profile(Profile("legacy-complete", ("core_objects", "objbench_objects", "extended_objects", "ceph_object_boundaries", "ceph_conditions", "ceph_metadata", "ceph_copy", "ceph_http_range", "ceph_tags", "ceph_headers", "ceph_buckets", "ceph_exact_listing", "ceph_delete_many", "ceph_exact_multipart", "ceph_multipart_copy", "ceph_get_part", "ceph_versioning", "ceph_bucket_config", "ceph_sse_customer", "ceph_bucket_controls", "ceph_sse_multipart", "ceph_encryption_objects", "ceph_bucket_policy", "ceph_acl", "ceph_bucket_inventory", "ceph_request_headers", "ceph_conditional_writes", "ceph_access", "ceph_object_interop", "ceph_policy_conditions", "ceph_metadata_edges", "ceph_checksum_attributes", "ceph_conditional_versions", "ceph_public_controls", "ceph_copy_contracts", "ceph_state_boundaries", "ceph_basic_boundaries", "ceph_policy_remaining", "ceph_logging_controls", "ceph_raw_http", "ceph_query_edges", "ceph_lifecycle_expiration", "ceph_lifecycle_transition", "ceph_lifecycle_marker_expiration", "ceph_object_locks", "ceph_logging_records", "ceph_bucket_names", "ceph_cloud_lifecycle", "ceph_logging_workloads", "ceph_logging_extensions", "ceph_metadata_tail", "ceph_encrypted_forms", "ceph_encryption_tail", "ceph_logging_policy_transitions", "ceph_logging_prefixes", "ceph_encrypted_copy_matrix", "ceph_logging_tenants", "ceph_logging_parallel", "ceph_presigned_urls", "ceph_atomic_streams", "ceph_iam_accounts", "ceph_iam_inline", "ceph_iam_tenant_actions", "ceph_iam_policy_entities", "ceph_logging_remaining", "ceph_s3_tail", "ceph_response_bodies", "ceph_s3_root_policy", "ceph_iam_root_keys", "ceph_sts_basic", "ceph_iam_role_policies", "ceph_iam_assumed_roles", "ceph_sts_web", "ceph_iam_oidc", "ceph_sts_callers", "ceph_iam_cross_resources", "ceph_select", "ceph_logging_remaining_extensions", "ceph_utils", "ceph_logging_remaining_parallel", "ceph_auxiliary", "ceph_logging_remaining_last"),
        "完善档：Ceph 场景迁移中；完整对齐状态与未迁移清单见 alignment.json", EXCLUSIONS))

    from .s3_profiles import register_profiles
    register_profiles(registry)
