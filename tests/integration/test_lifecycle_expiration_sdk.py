"""Validate lifecycle source stages through real SDKs and static HTTP documents; never emulate a lifecycle engine."""
from datetime import datetime,timedelta,timezone
from email.utils import format_datetime
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker
from osv.scenarios import Context
from osv.suites import Probe,run_cases,CaseNotRun
from osv.worker import Worker
from osv.ceph_lifecycle_expiration import CASES,LEGACY_PREFIX,interval,prefix_counts,wait,expiration_header

XML={"Content-Type":"application/xml"}
EMPTY_OBJECTS=b"<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>"
EMPTY_VERSIONS=b"<ListVersionsResult><IsTruncated>false</IsTruncated></ListVersionsResult>"
LC=(200,XML,b"<PutBucketLifecycleConfigurationOutput/>")
ENABLED=(200,XML,b"<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>")


def object_doc(keys):
    return ("<ListBucketResult><IsTruncated>false</IsTruncated>"+"".join("<Contents><Key>"+key+"</Key><Size>"+str(len(key))+"</Size></Contents>" for key in keys)+"</ListBucketResult>").encode()

def version_doc(rows,markers=()):
    # Only encode independently listed server rows; no expiration/rule/tag logic.
    return ("<ListVersionsResult><IsTruncated>false</IsTruncated>"+"".join("<Version><Key>"+key+"</Key><VersionId>"+ident+"</VersionId></Version>" for key,ident in rows)+"".join("<DeleteMarker><Key>"+key+"</Key><VersionId>"+ident+"</VersionId></DeleteMarker>" for key,ident in markers)+"</ListVersionsResult>").encode()

def scope_begin(versioned=False):
    return [(404,{},b""),(200,{},b"")]+([(200,{},b""),ENABLED] if versioned else [])

def put_result(number):
    return (200,{"x-amz-version-id":"service-v"+str(number)},b"")

def scope_end(keys=(),rows=None,markers=()):
    result=[(200,{},b"")]
    if rows is not None:
        if rows or markers:
            result += [(200,XML,version_doc(rows,markers))]+[(204,{},b"")]*(len(rows)+len(markers))
        result += [(200,XML,EMPTY_VERSIONS)]
    else:result += [(204,{},b"")]*len(keys)+[(200,XML,EMPTY_OBJECTS)]
    return result+[(204,{},b""),(404,{},b"")]

def stage_documents(name):
    """Provide literal independent service stages, without deriving expiration from request rules or simulating server state changes."""
    if name in ("prefix","prefix_v2"):
        keys=("expire1/foo","expire1/bar","keep2/foo","keep2/bar","expire3/foo","expire3/bar")
        stages=(keys,keys[2:],keys[2:],keys[2:4])
        return scope_begin()+[put_result(i) for i in range(6)]+[LC]+[(200,XML,object_doc(stage)) for stage in stages]+scope_end(keys)
    if name=="enabled_version":
        row=("test1/a","service-v0");marker=("test1/a","service-marker-0")
        return scope_begin(True)+[put_result(0),(204,{"x-amz-delete-marker":"true","x-amz-version-id":"service-marker-0"},b""),LC,(200,XML,version_doc([row],[marker]))]+scope_end(rows=[row],markers=[marker])
    if name in ("tag_one","tag_two","versioned_tag_two"):
        versioned=name=="versioned_tag_two";keys=("days1/tom",) if name=="tag_one" else ("days1/tom","days1/huck")
        setup=[]
        for number,key in enumerate(keys):setup += [put_result(number),(200,{},b"")]
        remaining=[("days1/tom","service-v0")]
        result=scope_begin(versioned)+setup+[LC]
        if name!="tag_one":result += [(200,XML,object_doc(["days1/tom"]))]
        return result+scope_end(keys,rows=remaining if versioned else None)
    if name in ("noncurrent_tag","newer_noncurrent"):
        setup=[]
        for number in range(10):
            setup += [put_result(number)]
            if name=="noncurrent_tag":setup += [(200,{},b"")]
        ten=[("myobject_","service-v"+str(i)) for i in range(10)]
        remaining=ten[-6:] if name=="newer_noncurrent" else ten[-1:]
        stages=[remaining] if name=="newer_noncurrent" else [ten,remaining]
        return scope_begin(True)+setup+[LC]+[(200,XML,version_doc(stage)) for stage in stages]+scope_end(rows=remaining)
    if name in ("size_gt","size_lt"):
        key="myobject_small" if name=="size_gt" else "myobject_big";ident="service-v0" if name=="size_gt" else "service-v1"
        return scope_begin(True)+[put_result(0),put_result(1),LC,(200,XML,object_doc([key]))]+scope_end(rows=[(key,ident)])
    if name=="date":
        keys=("past/foo","future/bar")
        return scope_begin()+[put_result(0),put_result(1),LC,(200,XML,object_doc(keys)),(200,XML,object_doc(["future/bar"]))]+scope_end(keys)
    if name=="days_zero":
        body=b"<Error><Code>InvalidArgument</Code><Message>independent-service-message</Message></Error>"
        return scope_begin()+[put_result(0),put_result(1),(400,XML,body)]+scope_end(("days0/foo","days0/bar"))
    if name.startswith("header_"):
        basis=datetime.utcnow() if name in ("header_put","header_head") else datetime.now(None)
        expiration=format_datetime((basis+timedelta(hours=36)).replace(tzinfo=timezone.utc),usegmt=True)
        present={"x-amz-expiration":'expiry-date="'+expiration+'", rule-id="rule1"'}
        result=scope_begin()+[LC]
        if name=="header_put":result += [(200,present,b"")];keys=("days1/foo",)
        elif name=="header_head":result += [put_result(0),(200,present,b"")];keys=("days1/foo",)
        else:
            keys=("obj_key1",);result += [put_result(0),(200,{},b""),(200,present if name=="header_tag" else {},b"")]
            if name=="header_tag":result += [LC,(200,{},b"")]
        return result+scope_end(keys)
    if name=="noncurrent":
        six=[("test1/a","service-v"+str(i)) for i in range(3)]+[("test2/abc","service-v"+str(i)) for i in range(3,6)]
        remaining=six[2:]
        return scope_begin(True)+[put_result(i) for i in range(6)]+[(200,XML,version_doc(six)),LC,(200,XML,version_doc(remaining))]+scope_end(rows=remaining)
    if name in ("marker","marker_days"):
        row=("test1/a","service-v0");marker=("test1/a","service-marker-0")
        result=scope_begin(True)+[put_result(0)]
        if name=="marker":result += [put_result(1)]
        result += [(204,{"x-amz-delete-marker":"true","x-amz-version-id":"service-marker-0"},b"")]
        if name=="marker":
            second=("test2/abc","service-v1");second_marker=("test2/abc","service-marker-1")
            result += [(204,{"x-amz-delete-marker":"true","x-amz-version-id":"service-marker-1"},b""),(200,XML,version_doc([row,second],[marker,second_marker])),LC,(200,XML,version_doc([second],[second_marker]))]
            return result+scope_end(rows=[second],markers=[second_marker])
        result += [LC,(200,XML,version_doc([],[marker])),(200,XML,EMPTY_VERSIONS)]
        return result+scope_end(rows=[])
    if name=="multipart":
        init=lambda ident:(200,XML,("<InitiateMultipartUploadResult><UploadId>"+ident+"</UploadId></InitiateMultipartUploadResult>").encode())
        listing=lambda ids:(200,XML,("<ListMultipartUploadsResult>"+"".join("<Upload><Key>"+key+"</Key><UploadId>"+ident+"</UploadId></Upload>" for key,ident in ids)+"</ListMultipartUploadsResult>").encode())
        # ExitStack aborts second upload first; the first has already expired.
        missing=(404,XML,b"<Error><Code>NoSuchUpload</Code><Message>expired</Message></Error>")
        return scope_begin()+[init("service-upload-1"),init("service-upload-2"),listing([("test1/a","service-upload-1"),("test2/","service-upload-2")]),LC,listing([("test2/","service-upload-2")]),(204,{},b""),missing]+scope_end(("test1/a","test2/"))
    raise ValueError(name)

def assert_original_rule_documents(test,name,documents):
    """Compare independent literal source leaf values with actual SDK XML, including legacy-vs-filter paths and every boundary parameter."""
    def leaves(node,prefix=""):
        children=list(node)
        if not children:return [(prefix,node.text or "")]
        values=[]
        for child in children:values.extend(leaves(child,prefix+"/"+child.tag.rsplit("}",1)[-1]))
        return values
    def rule(ident="rule1",**values):
        return sorted([("/ID",ident),("/Status","Enabled")]+[("/"+key.replace(".","/"),str(value)) for key,value in values.items()])
    expected={
      "prefix":[rule(Prefix="expire1/",**{"Expiration.Days":1}),rule("rule2",Prefix="expire3/",**{"Expiration.Days":5})],
      "enabled_version":[rule(Prefix="test1/",**{"Expiration.Days":1})],
      "date":[rule(Prefix="past/",**{"Expiration.Date":"2015-01-01T00:00:00Z"}),rule("rule2",Prefix="future/",**{"Expiration.Date":"2030-01-01T00:00:00Z"})],
      "days_zero":[rule(Prefix="days0/",**{"Expiration.Days":0})],
      "header_put":[rule(Prefix="days1/",**{"Expiration.Days":1})],
      "header_tag":[rule(**{"Filter.Tag.Key":"key1","Filter.Tag.Value":"tag1","Expiration.Days":1})],
      "noncurrent_tag":[rule("rule_tag1",**{"Filter.Prefix":"","Filter.Tag.Key":"vidushi","Filter.Tag.Value":"mishra","NoncurrentVersionExpiration.NoncurrentDays":4})],
      "newer_noncurrent":[rule("newer_noncurrent1",**{"Filter.Prefix":"","NoncurrentVersionExpiration.NoncurrentDays":1,"NoncurrentVersionExpiration.NewerNoncurrentVersions":5})],
      "size_gt":[rule("object_gt1",**{"Filter.Prefix":"","Filter.ObjectSizeGreaterThan":2000,"Expiration.Days":1})],
      "size_lt":[rule("object_lt1",**{"Filter.Prefix":"","Filter.ObjectSizeLessThan":2000,"Expiration.Days":1})],
      "noncurrent":[rule(Prefix="test1/",**{"NoncurrentVersionExpiration.NoncurrentDays":2})],
      "marker":[rule(Prefix="test1/",**{"NoncurrentVersionExpiration.NoncurrentDays":1,"Expiration.ExpiredObjectDeleteMarker":"true"})],
      "marker_days":[rule(Prefix="test1/",**{"NoncurrentVersionExpiration.NoncurrentDays":1,"Expiration.Days":5})],
      "multipart":[rule(Prefix="test1/",**{"AbortIncompleteMultipartUpload.DaysAfterInitiation":2})],
    }
    expected["prefix_v2"]=expected["prefix"]
    expected["header_head"]=expected["header_put"]
    tag=rule("rule_tag1",**{"Expiration.Days":1,"Filter.Prefix":"days1/","Filter.Tag.Key":"tom","Filter.Tag.Value":"sawyer"})
    expected["tag_one"]=[tag]
    combined=sorted(tag+[("/Filter/And/Prefix","days1"),("/Filter/And/Tag/Key","huck"),("/Filter/And/Tag/Value","finn")])
    expected["tag_two"]=[combined];expected["versioned_tag_two"]=[combined]
    # AWS serializes list-member Tags as repeated Tag XML nodes.
    expected["header_and_tags"]=[sorted(rule(**{"Expiration.Days":1})+[("/Filter/And/Tag/Key","key1"),("/Filter/And/Tag/Value","tag1"),("/Filter/And/Tag/Key","key5"),("/Filter/And/Tag/Value","tag6")])]
    first=[sorted(leaves(node)) for node in documents[0].findall("{*}Rule")]
    if name=="date":
        # Equivalent ISO-8601 fractions differ by SDK serializer version.
        first=[sorted((path,datetime.fromisoformat(value.replace("Z","+00:00")).strftime("%Y-%m-%dT%H:%M:%SZ") if path=="/Expiration/Date" else value) for path,value in row) for row in first]
    test.assertEqual(first,expected[name])
    test.assertEqual(len(documents),2 if name=="header_tag" else 1)
    if name=="header_tag":test.assertEqual([sorted(leaves(node)) for node in documents[1].findall("{*}Rule")],[rule(**{"Filter.Tag.Key":"key2","Filter.Tag.Value":"tag1","Expiration.Days":1})])

@unittest.skipUnless(os.environ.get("OSV_RUN_LIFECYCLE_EXPIRATION_TESTS")=="1","enable after building actual Java/C++ SDK workers")
class LifecycleExpirationSdkTest(unittest.TestCase):
    """Separate source-oracle/SDK wire checks from real lifecycle-service certification and preserve missing backend prerequisites."""
    @classmethod
    def setUpClass(cls):
        """Start only the common local service fixture with explicit dummy credentials and actual SDK workers."""
        from test_s3_sdk import SdkIntegrationTest
        SdkIntegrationTest.setUpClass.__func__(cls)

    def test_default_profile_does_not_invent_accelerated_service_time(self):
        """Run all 20 source cases without an accelerated-service declaration; expect 15 background cases not_run, genuine immediate-case results retained and no owned resources left."""
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker(target,30)
                try:
                    context=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    results=run_cases(context,CASES)
                    self.assertEqual(len(results),20)
                    fail={"ceph_lc_expiration_header_tag"}
                    not_run={"ceph_lc_expiration_"+name for name in ("prefix","prefix_v2","enabled_version","tag_one","tag_two","versioned_tag_two","noncurrent_tag","newer_noncurrent","size_gt","size_lt","date","noncurrent","marker","marker_days","multipart")}
                    if "lifecycle_legacy_prefix" in worker.capabilities:
                        fail.update("ceph_lc_expiration_"+name for name in ("days_zero","header_put","header_head"))
                    else:not_run.update("ceph_lc_expiration_"+name for name in ("days_zero","header_put","header_head"))
                    self.assertEqual({row["case"] for row in results if row["status"]=="fail"},fail)
                    self.assertEqual({row["case"] for row in results if row["status"]=="not_run"},not_run)
                    self.assertEqual({row["case"] for row in results if row["status"]=="pass"},{"ceph_lc_expiration_header_and_tags"})
                    print("LIFECYCLE_DEFAULT_DIAGNOSTICS",target["language"],results,flush=True)
                finally:worker.close()
                self.assertEqual([row["Name"] for row in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_all_original_stages_consume_independent_real_sdk_documents(self):
        """Replay independent static HTTP documents through each complete source scenario; expect original rules/data/phase oracles and genuine cleanup, without claiming any background lifecycle engine was tested."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            for case in CASES:
                name=case.name.removeprefix("ceph_lc_expiration_");fixtures=stage_documents(name)
                with self.subTest(language=language,case=name),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                    directory=Path(temporary);context=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{"lifecycle_debug_interval_seconds":0.001})
                    if name in LEGACY_PREFIX and "lifecycle_legacy_prefix" not in worker.capabilities:
                        result=run_cases(context,[case])[0]
                        self.assertEqual(result["status"],"not_run")
                        self.assertEqual(result["missing_operations"],["lifecycle_legacy_prefix"])
                        self.assertEqual(server.requests,[])
                        continue
                    with patch("osv.ceph_lifecycle_expiration.wait",wraps=wait) as observed_wait:
                        evidence=case.execute(Probe(context))
                    expected_waits={"prefix":[3,1,3],"prefix_v2":[3,1,3],"enabled_version":[3],"tag_one":[3],"tag_two":[3],"versioned_tag_two":[3],"noncurrent_tag":[2,5],"newer_noncurrent":[2],"size_gt":[10],"size_lt":[2],"date":[3],"noncurrent":[5],"marker":[7],"marker_days":[2,4],"multipart":[5]}
                    self.assertEqual([call.args[0] for call in observed_wait.call_args_list],[factor*0.001 for factor in expected_waits.get(name,[])])
                    self.assertEqual(len(server.requests),len(fixtures))
                    self.assertEqual(server.requests[-1][0],"HEAD")
                    documents=[ET.fromstring(request[3]) for request in server.requests if request[0]=="PUT" and "lifecycle" in request[1]]
                    self.assertTrue(documents)
                    assert_original_rule_documents(self, name, documents)
                    if name=="tag_one":
                        self.assertIn("no post-wait object listing",evidence["source_limitation"])
                        self.assertEqual(sum(request[0]=="GET" and request[1].split("?",1)[0].count("/")==1 for request in server.requests),1)
                    if name=="date":
                        self.assertEqual([datetime.fromisoformat(node.text.replace("Z","+00:00")) for node in documents[0].findall(".//{*}Expiration/{*}Date")],[datetime(2015,1,1,tzinfo=timezone.utc),datetime(2030,1,1,tzinfo=timezone.utc)])

    def test_missing_or_invalid_interval_never_reaches_http(self):
        """Provide missing, zero/negative, nonnumeric, boolean or nonfinite backend intervals; expect explicit prerequisite/config errors before bucket creation or sleep."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3") as (worker,server):
                directory=Path(temporary)
                for value in (None,0,-1,True,"10",float("nan"),float("inf"),10**1000):
                    context=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{"lifecycle_debug_interval_seconds":value})
                    with self.assertRaises(CaseNotRun if value is None else ValueError):prefix_counts(Probe(context))
                self.assertEqual(server.requests,[])

    def test_actual_expiration_headers_preserve_day_rule_and_format_boundaries(self):
        """Feed real SDK HEAD headers into the original parser; expect missing/wrong-rule/early-day rejection, 1-day floor acceptance and malformed-format failures without fabricating responses."""
        start=datetime(2026,1,1,12,0,0)
        rows=[({},False),({"x-amz-expiration":'expiry-date="Fri, 02 Jan 2026 12:00:00 GMT", rule-id="rule1"'},True),({"x-amz-expiration":'expiry-date="Sat, 03 Jan 2026 11:59:59 GMT", rule-id="rule1"'},True),({"x-amz-expiration":'expiry-date="Fri, 02 Jan 2026 11:59:59 GMT", rule-id="rule1"'},False),({"x-amz-expiration":'expiry-date="Sat, 03 Jan 2026 12:00:00 GMT", rule-id="rule1"'},False),({"x-amz-expiration":'expiry-date="Fri, 02 Jan 2026 12:00:00 GMT", rule-id="other-rule"'},False),({"x-amz-expiration":"malformed"},AttributeError),({"x-amz-expiration":'expiry-date="not-a-date", rule-id="rule1"'},ValueError)]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,headers,b"") for headers,_ in rows],"s3") as (worker,server):
                for headers,expected in rows:
                    response=worker.call({"op":"head_http","bucket":"osv-existing-independent-bucket","key":"key"})
                    if isinstance(expected,type):
                        with self.assertRaises(expected):expiration_header(response,start,"rule1",1)
                    else:self.assertEqual(expiration_header(response,start,"rule1",1),expected)
                self.assertEqual(len(server.requests),len(rows))
