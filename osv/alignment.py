"""Audit actual upstream identities independently of the number of local case names."""
from importlib.resources import files
import json
import re

JUICE_FUNCTIONAL={
 "create a bucket":["bucket_create_idempotent"],"put an object":["object_roundtrip"],
 "get an object":["object_roundtrip"],"get non-exist":["missing_get"],
 "get partial object":["hello_partial_ranges","range_end_overflow","juicefs_range_start_overflow"],
 "head an object":["object_roundtrip","list_object_fields"],
 "delete an object":["delete_existing"],"delete non-exist":["delete_missing"],
 "list objects":["list_object_fields","list_after_marker","list_sorted_100"],
 "special key":["special_control_key"],"put a big object":["large_object_put"],
 "put an empty object":["empty_roundtrip","slash_suffixed_key"],"multipart upload":["multipart_grow_shrink"]}
FILESYSTEM={"change owner/group","change permission","change mtime","chmod","chown","chtimes"}


def inventory():
    return json.loads(files("osv").joinpath("data/upstream.json").read_text())


def audit(config,registry):
    """Never turn a name/reference match into a claim of full upstream parameter/assertion equivalence."""
    upstream=inventory();selected=config["workload"]["compatibility_suites"]
    review_data=json.loads(files("osv").joinpath("data/ceph_reviews.json").read_text())
    reviews={row["source_id"]:row for row in review_data["reviews"]} if review_data["revision"]==upstream["ceph"]["revision"] else {}
    catalog=[case for name in selected for case in registry.suites[name].catalog]
    names={case["case"] for case in catalog}
    rows=[]
    for row in upstream["juicefs"]["cases"]:
        row={**row,"project":"juicefs","implementations":[]}
        if row["name"] in FILESYSTEM:
            row.update(status="not_applicable",reason="对象存储 SDK 不提供 POSIX owner/mode/mtime；与 objbench 的文件系统可选能力区分")
        elif row["kind"]=="functional":
            required=JUICE_FUNCTIONAL.get(row["name"],[])
            row["implementations"]=[name for name in required if name in names]
            row["status"]="aligned" if required and len(row["implementations"])==len(required) else "not_migrated"
        else:
            row["status"]="aligned" if config["workload"]["mode"] in ("performance", "all") else "not_selected"
            row["implementations"]=["objbench:"+row["name"]] if row["status"]=="aligned" else []
        rows.append(row)
    for row in upstream["ceph"]["cases"]:
        local=[case["case"] for case in catalog if row["name"] in re.findall(r"\btest_\w+\b",case["reference"])]
        shadowed=not row.get("active_definition",True)
        review=reviews.get(row["id"],{})
        aligned=review.get("implementation") in local and review.get("source_sha256")==row["sha256"]
        rows.append({**row,"project":"ceph","status":"not_applicable" if shadowed else "aligned" if aligned else "partial" if local else "not_migrated","implementations":[] if shadowed else local,"reviewed_implementation":review.get("implementation") if aligned and not shadowed else None,
                     "reason":"上游同名定义被后续函数覆盖，不是实际收集的用例" if shadowed else "已核对固定版本输入与全部断言；使用对象前缀或独立桶隔离，实际 SDK 执行结果另见报告" if aligned else "已迁移相关场景；仍须核对完整参数组合、响应字段、权限与协议版本" if local else "尚未迁移"})
    summaries=[]
    for project in ("juicefs","ceph"):
        selected=[row for row in rows if row["project"]==project]
        counts={status:sum(row["status"]==status for row in selected) for status in ("aligned","partial","not_migrated","not_applicable","not_selected")}
        summaries.append({"project":project,"revision":upstream[project]["revision"],"total":len(selected),**counts,
                          "complete":all(row["status"] in ("aligned","not_applicable") for row in selected)})
    return {"schema_version":1,"counting_unit":upstream["counting_unit"],"summary":summaries,"cases":rows,
            "qualification":"aligned 指已实现的场景/断言及负载形态；实际执行、参数缩放和云端结果必须另行核对"}
