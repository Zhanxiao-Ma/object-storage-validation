"""Separate standard S3 coverage from upstream vendor and control-service contracts."""
from dataclasses import replace
from .scenarios import Profile, Suite

QUERY_EXTENSIONS = frozenset({"ceph_query_head_usage", "ceph_query_head_extended",
                             "ceph_query_unordered_v1", "ceph_query_unordered_v2"})
NAMESPACE_EXTENSIONS = frozenset({"ceph_remaining_policy_tenant_global", "ceph_remaining_policy_tenant_owned",
                                 "ceph_logging_tenant_standard", "ceph_logging_remaining_tenant_j"})
LOG_DELIVERY_SUITES = frozenset({"ceph_logging_workloads", "ceph_logging_prefixes", "ceph_logging_tenants",
    "ceph_logging_parallel", "ceph_logging_remaining", "ceph_logging_records", "ceph_logging_controls"})
LOG_DELIVERY_CASES = frozenset({"ceph_logging_transition_wildcard_objects", "ceph_logging_transition_permission_s"})
STANDARD_EXPIRATION_SUFFIXES = ("days_zero", "header_put", "header_head", "header_tag", "header_and_tags")


def category(suite, row):
    """Classify audited executable contracts; source project names alone are not evidence of binding."""
    name, operations = row["case"], row["operations"]
    if any(op.startswith(("api.iam.", "api.sts.", "api.sns.", "api.s3control.")) for op in operations):
        return "excluded"
    if (suite == "ceph_cloud_lifecycle" or name == "ceph_body_account_usage" or name in QUERY_EXTENSIONS | NAMESPACE_EXTENSIONS
            or any(op in {"ceph_logging_wire_extensions", "ceph_bucket_logging_extensions", "api.PostBucketLogging"}
                   or op.startswith("api.ceph.") for op in operations)):
        return "excluded"
    if (suite in LOG_DELIVERY_SUITES or name in LOG_DELIVERY_CASES
            or suite in {"ceph_lifecycle_transition", "ceph_lifecycle_marker_expiration", "ceph_utils"}
            or (suite == "ceph_lifecycle_expiration" and not name.endswith(STANDARD_EXPIRATION_SUFFIXES))):
        return "excluded"
    return "complete"


def subset(registry, original, rows, category_name):
    """Gate case execution before IO and expose exactly the selected catalog to the runner."""
    if len(rows) == len(original.catalog):
        return original.name
    selected = frozenset(row["case"] for row in rows)
    prefix = {"complete": "s3_", "normal": "normal_"}[category_name]
    name = prefix + original.name.removeprefix("ceph_")
    registry.add_suite(Suite(name, frozenset(op for row in rows for op in row["operations"]),
        lambda ctx: original.execute(replace(ctx, selected_cases=selected)), tuple(rows), case_capabilities=True))
    return name


NORMAL_OBJECT_SUITES = frozenset({"ceph_copy", "ceph_buckets", "ceph_exact_multipart"})
NORMAL_ADDITIONAL_CASES = frozenset({
    "ceph_delete_many_v2_repeat", "ceph_delete_many_v2_limit",
    "s3_delete_many_quiet", "s3_delete_many_verbose",
    "s3_lifecycle_configuration_abort_multipart",
})


def register_normal_profile(registry):
    """Reuse generic common-API cases in the default profile while keeping its catalog a strict subset of complete."""
    original = registry.profiles["normal"]
    names = list(original.suites)
    selected = set()
    for name in registry.profiles["complete"].suites:
        if name in names:
            continue
        suite = registry.suites[name]
        rows = [row for row in suite.catalog if name in NORMAL_OBJECT_SUITES
            or row["case"] in NORMAL_ADDITIONAL_CASES
            or row["case"].startswith("ceph_exact_list_v2_")]
        if rows:
            names.append(subset(registry, suite, rows, "normal"))
            selected.update(row["case"] for row in rows)
    missing = NORMAL_ADDITIONAL_CASES - selected
    if missing:
        raise ValueError("normal profile required common-API cases missing: " + ", ".join(sorted(missing)))
    registry.profiles["normal"] = Profile("normal", tuple(names),
        "正常档：常用 S3 对象 API；含基础读写、复制、批量/静默删除、桶检查、分页列举、分片上传及未完成分片生命周期配置",
        ("版本控制、对象锁、加密、ACL/Policy、Select 等扩展场景见 complete",
         "生命周期只验证配置；缺少创建桶或配置权限会保留实际失败/未执行原因"))


def register_profiles(registry, original_suites):
    """Expose only standard S3 profiles; excluded vendor/control cases have no built-in profile."""
    suites = []
    for suite_name in original_suites:
        suite = registry.suites[suite_name]
        rows = [row for row in suite.catalog if category(suite_name, row) == "complete"]
        if rows:
            suites.append(subset(registry, suite, rows, "complete"))
    from .s3_configuration import register
    register(registry)
    suites.append("s3_configuration")
    from .s3_delete_compatibility import register as register_delete_compatibility
    register_delete_compatibility(registry)
    suites.append("s3_delete_compatibility")
    registry.add_profile(Profile("complete", tuple(suites),
        "完整档：通用 S3 协议兼容性；独立身份可选，不依赖厂商扩展或外部控制服务",
        ("不包含 Ceph/RGW 专有协议、加速生命周期及原始日志交付假设",
         "不包含 IAM、STS、SNS、S3Control 独立服务",
         "生命周期验证配置与响应头，不推断后台执行完成；日志验证配置，不要求固定时间交付",)))
    register_normal_profile(registry)
