"""Translate Ceph conditional-read vectors through explicit real-worker capabilities."""
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

PAST=783459811000  # Sat, 29 Oct 1994 19:43:31 GMT
FUTURE=4128522211000  # Fri, 29 Oct 2100 19:43:31 GMT


def conditional(p,field,succeeds):
    """Use actual stored bytes and ETag/mtime, never a synthesized storage response."""
    payload=b"bar";p.put(payload)
    head=p.call("head")
    tag='"'+head["etag"].strip('"')+'"'
    value=tag if field=="if_match" and succeeds or field=="if_none_match" and not succeeds else '"ABCORZ"'
    if field=="if_modified_since_ms":value=PAST if succeeds else (head["last_modified_ms"]//1000+1)*1000
    if field=="if_unmodified_since_ms":value=FUTURE if succeeds else PAST
    p.ctx.output.unlink(missing_ok=True)
    response=p.raw("get_conditional",output_path=str(p.ctx.output),**{field:value})
    if succeeds:
        assert_equal(response["ok"],True)
        assert_equal(p.ctx.output.read_bytes(),payload)
    else:
        assert_equal(response["ok"],False)
        assert_equal(response.get("error_kind"),"local_error" if p.ctx.worker.info.get("api")=="local" else "sdk_error")
        expected=304 if field in ("if_none_match","if_modified_since_ms") else 412
        if "status_code" in response:assert_equal(response["status_code"],expected)
        else:
            codes=("NotModified","304") if expected==304 else ("PreconditionFailed",)
            if response.get("error_code") not in codes:
                raise AssertionError("SDK does not expose expected condition status/code: "+str(response))
        if expected==412 and response.get("error_code"):assert_equal(response["error_code"],"PreconditionFailed")


VECTORS=(
 ("ifmatch_good","if_match",True),("ifmatch_failed","if_match",False),
 ("ifnonematch_good","if_none_match",False),("ifnonematch_failed","if_none_match",True),
 ("ifmodifiedsince_good","if_modified_since_ms",True),("ifmodifiedsince_failed","if_modified_since_ms",False),
 ("ifunmodifiedsince_good","if_unmodified_since_ms",False),("ifunmodifiedsince_failed","if_unmodified_since_ms",True),
)
CASES=tuple(case("ceph_get_"+name,"条件读取 "+name,
                "成功时完整读取 bar；拒绝时是实际 SDK 的 304/412，可观测字段不能伪造",
                "put head get_conditional",lambda p,field=field,success=success:conditional(p,field,success),
                "test_get_object_"+name,CEPH) for name,field,success in VECTORS)


def register(registry):
    registry.add_suite(Suite("ceph_conditions",frozenset().union(*(c.operations for c in CASES)),
        lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
