"""Exercise standard S3 contracts also used by upstream cleanup helpers."""
from .bucket_scope import BucketScope, SCOPE_OPERATIONS
from .version_scope import VersionScope, VERSION_SCOPE_OPERATIONS
from .ceph_bucket_config import api
from .ceph_object_locks import locked, put, retention, BYPASS
from .scenarios import Suite, assert_equal
from .suites import case, run_cases

SOURCE = "https://github.com/ceph/s3-tests/blob/5b3987116eca0b4b8fa57d6b10f3db57bab196b6/s3tests/functional/__init__.py"


def check_result(result, deleted, errors=()):
    """Require actual HTTP success, exact acknowledgments and complete per-object failures."""
    assert_equal(result["status_code"], 200)
    assert_equal(sorted((r["key"], r.get("version_id")) for r in result["deleted"]), sorted(deleted))
    assert_equal(sorted((r["key"], r.get("version_id"), r["code"]) for r in result["errors"]), sorted(errors))
    assert_equal(result["errors_present"], bool(errors))


def ordinary(p, quiet):
    with BucketScope(p) as bucket:
        names = ["plain", "special <&+% 中文"]
        for name in names:
            bucket.put(name, name.encode())
        identifiers = [{"key": name} for name in names + ["never-created"]]
        for _ in range(2):
            result = bucket.call("delete_many", objects=identifiers, quiet=quiet)
            for name in names + ["never-created"]:
                assert_equal(bucket.raw("head", key=name).get("status_code"), 404)
            assert_equal(bucket.call("list", prefix="")["keys"], [])
            check_result(result, [] if quiet else [(r["key"], None) for r in identifiers])


def versions(p):
    with VersionScope(p) as bucket:
        bucket.call("versioning_put", status="Enabled")
        first = put(bucket, p, "history")["version_id"]
        latest = put(bucket, p, "history")["version_id"]
        other = put(bucket, p, "marked")["version_id"]
        marker = bucket.call("delete_version", key="marked")["version_id"]
        result = bucket.call("delete_many_versions", quiet=True, objects=[
            {"key": "history", "version_id": first}, {"key": "marked", "version_id": marker}])
        page = bucket.call("versions_list")
        assert_equal({(r["key"], r["version_id"]) for r in page["versions"]}, {("history", latest), ("marked", other)})
        assert_equal(page["delete_markers"], [])
        assert_equal(bucket.raw("head_version", key="history", version_id=first).get("status_code"), 404)
        for name, ident in (("history", latest), ("marked", other)):
            bucket.call("get_version", key=name, version_id=ident, output_path=str(p.ctx.output))
            assert_equal(p.ctx.output.read_bytes(), b"abc")

        check_result(result, [])


def protected(p, governance):
    # Holds can always be removed by the owner; long governance retention is
    # permitted only after the existing real same-key bypass cleanup proof.
    with locked(p, probe=governance) as bucket:
        retained = put(bucket, p, "file1")["version_id"]
        free = put(bucket, p, "free")["version_id"]
        if governance:
            api(bucket, "PutObjectRetention", Key="file1", VersionId=retained, Retention=retention())
        else:
            api(bucket, "PutObjectLegalHold", Key="file1", VersionId=retained, LegalHold={"Status": "ON"})
        identifiers = [{"key": "file1", "version_id": retained}, {"key": "free", "version_id": free}]
        result = bucket.call("delete_many_versions", objects=identifiers, quiet=True)
        first_result = result
        assert_equal(bucket.raw("head_version", key="free", version_id=free).get("status_code"), 404)
        bucket.call("get_version", key="file1", version_id=retained, output_path=str(p.ctx.output))
        assert_equal(p.ctx.output.read_bytes(), b"abc")
        if not governance:
            api(bucket, "PutObjectLegalHold", Key="file1", VersionId=retained, LegalHold={"Status": "OFF"})
        result = bucket.call("delete_many_versions", objects=identifiers[:1], quiet=True,
                             **({"wire_headers": BYPASS} if governance else {}))
        assert_equal(bucket.raw("head_version", key="file1", version_id=retained).get("status_code"), 404)
        # Check all real state changes and bypass behavior before reporting an
        # error-code difference, so it cannot hide lost successes or protection.
        check_result(result, [])
        check_result(first_result, [], [("file1", retained, "AccessDenied")])


def version_cursor(p):
    with VersionScope(p) as bucket:
        bucket.call("versioning_put", status="Enabled")
        expected = set()
        for name in ("same-key", "same-key", "z-next-key"):
            ident = put(bucket, p, name)["version_id"]
            expected.add(("version", name, ident))
        marker = bucket.call("delete_version", key="same-key")["version_id"]
        expected.add(("marker", "same-key", marker))
        observed = set()
        cursors = set()
        options = {}
        for _ in range(len(expected) + 1):
            page = bucket.call("versions_list", max_keys=1, **options)
            rows = [(kind, row["key"], row["version_id"]) for field, kind in
                    (("versions", "version"), ("delete_markers", "marker")) for row in page[field]]
            if len(rows) > 1 or (not rows and page["truncated"]):
                raise AssertionError("invalid version page: " + str(page))
            assert_equal(bool(observed.intersection(rows)), False)
            observed.update(rows)
            if not page["truncated"]:
                assert_equal(observed, expected)
                return
            cursor = (page["next_key_marker"], page["next_version_id_marker"])
            if not cursor[0] or cursor in cursors:
                raise AssertionError("version listing cursor did not advance")
            cursors.add(cursor)
            options = {"key_marker": cursor[0]}
            if cursor[1] is not None:
                options["version_id_marker"] = cursor[1]
        raise AssertionError("version listing exceeded independent expected page count")


PLAIN = SCOPE_OPERATIONS + " put head delete_many"
VERSIONED = VERSION_SCOPE_OPERATIONS + " put_version versioning_put head_version get_version delete_many_versions"
LOCKED = VERSIONED + " bucket_create_locked api.PutObjectLegalHold api.PutObjectRetention"
CASES = (
    case("s3_delete_many_quiet", "静默批量删除现存和不存在对象并重复请求", "对象确已删除，Deleted/Errors 均为空，特殊键完整往返", PLAIN, lambda p: ordinary(p, True), "nuke_bucket", SOURCE),
    case("s3_delete_many_verbose", "显式非静默批量删除对照", "每个现存或不存在键均返回 Deleted，重复请求幂等", PLAIN, lambda p: ordinary(p, False), "nuke_bucket", SOURCE),
    case("s3_delete_many_quiet_versions", "静默删除历史版本和删除标记", "指定版本和标记被移除，其他版本和正文完整保留，成功项省略", VERSIONED, versions, "nuke_bucket", SOURCE),
    case("s3_delete_many_quiet_legal_hold", "静默删除同时包含受保护和可删除版本", "成功项省略，AccessDenied 保留 key/version，成功版本消失且受保护正文不变，解除保护后可删除", LOCKED, lambda p: protected(p, False), "nuke_bucket", SOURCE),
    case("s3_delete_many_quiet_governance_bypass", "静默批量删除与治理保留绕过", "无 bypass 时返回对象级 AccessDenied；真实绕过后成功项省略且版本消失", LOCKED, lambda p: protected(p, True), "nuke_bucket", SOURCE),
    case("s3_versions_cursor_walk", "同键历史版本及删除标记跨页遍历", "真实 KeyMarker/VersionIdMarker 进入下一次 SDK 请求，所有生产者 ID 恰好出现一次", VERSION_SCOPE_OPERATIONS + " versioning_put put_version", version_cursor, "list_versions", SOURCE),
)


def register(registry):
    registry.add_suite(Suite("s3_delete_compatibility", frozenset().union(*(c.operations for c in CASES)),
        lambda ctx: run_cases(ctx, CASES), tuple(c.metadata() for c in CASES), case_capabilities=True))
