"""Inspect the genuine final SDK HTTP response without reconstructing protocol metadata."""
import base64
from xml.etree import ElementTree as ET
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .scenarios import Suite,require_ok,assert_equal
from .suites import case,run_cases,CEPH


def body(result):
    if result.get('response_body_capture_error'):raise AssertionError(result['response_body_capture_error'])
    if not isinstance(result.get('response_body_b64'),str):raise AssertionError('actual SDK response body was not captured')
    return base64.b64decode(result['response_body_b64'],validate=True)


def xml_to_json(element):
    # Original source semantics deliberately keep last duplicate child and text
    # as-is. Do not normalize XML namespaces, numbers or whitespace.
    result={}
    for child in list(element):result[child.tag]=xml_to_json(child) if len(list(child))>0 else child.text or ''
    return result


def account_usage(p):
    """Append the original bare usage flag to actual SDK ListBuckets; expect all five literal quota fields in the genuine response XML."""
    result=require_ok(p.ctx.worker.call({'op':'api.ListBuckets','input':{},'wire_query':{'usage':True},'capture_response_body':True}))
    parsed=xml_to_json(ET.fromstring(body(result).decode('utf-8')));summary=parsed['Summary']
    for key,value in (('QuotaMaxBytes','-1'),('QuotaMaxBuckets','1000'),('QuotaMaxObjCount','-1'),('QuotaMaxBytesPerBucket','-1'),('QuotaMaxObjCountPerBucket','-1')):assert_equal(summary[key],value)
    return {'quota_source':'actual_SDK_HTTP_body'}


def request_id(p):
    """Read an actual missing-object error body; expect its non-null RequestId to equal the official SDK exception metadata request ID."""
    with BucketScope(p) as bucket:
        result=bucket.raw('get',key='bar',output_path=str(p.ctx.output),capture_response_body=True)
        assert_equal(result.get('ok'),False)
        value=ET.fromstring(body(result)).find('.//RequestId').text
        assert_equal(value is not None,True);assert_equal(value,result.get('request_id'))
    return {'request_id_oracle':'raw_error_XML_vs_official_SDK_metadata'}


CASES=(case('ceph_body_account_usage','Ceph account usage真实响应','原始ListBuckets?usage与全部五个quota字面断言；只解析实际HTTP响应，不从typed默认值重建','api.ListBuckets wire_query response_body_capture',account_usage,'test_account_usage',CEPH),case('ceph_body_error_request_id','Ceph error RequestId真实响应','保留源仅ClientError断言与XML RequestId非null/SDKmetadata相等；不额外增强为某个固定错误码',SCOPE_OPERATIONS+' get response_body_capture sdk_request_id',request_id,'test_object_requestid_matches_header_on_error',CEPH))


def register(registry):
    registry.add_suite(Suite('ceph_response_bodies',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
