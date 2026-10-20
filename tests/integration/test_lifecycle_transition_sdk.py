"""Check original lifecycle storage-class phase consumers through real SDKs and independent static service documents."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker
from test_lifecycle_expiration_sdk import XML,LC,ENABLED,scope_begin,scope_end,put_result,object_doc
from osv.ceph_lifecycle_transition import CASES
from osv.ceph_lifecycle_expiration import wait
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.worker import Worker


def version_rows(rows):
    return ("<ListVersionsResult><IsTruncated>false</IsTruncated>"+"".join("<Version><Key>"+key+"</Key><VersionId>"+ident+"</VersionId><StorageClass>"+sc+"</StorageClass></Version>" for key,ident,sc in rows)+"</ListVersionsResult>").encode()

def stages(name):
    if name in ("current","current_multiple"):
        keys=("expire1/foo","expire1/bar","keep2/foo","keep2/bar","expire3/foo","expire3/bar")
        first=[(key,"null","STANDARD_IA" if key.startswith("expire1/") else "STANDARD") for key in keys]
        last=[(key,"null",("GLACIER" if key.startswith("expire1/") else "STANDARD") if name=="current_multiple" else ("GLACIER" if key.startswith("expire3/") else sc)) for key,_,sc in first]
        return scope_begin()+[put_result(i) for i in range(6)]+[LC,(200,XML,object_doc(keys))]+[(200,XML,version_rows(rows)) for rows in (first,first,last)]+scope_end(keys)
    if name=="noncurrent":
        initial=[("test1/a","null","STANDARD")]+[("test1/a","service-v"+str(i),"STANDARD") for i in range(2)]+[("test1/b","service-v"+str(i),"STANDARD") for i in range(2,5)]
        first=[(key,ident,"STANDARD" if ident in ("service-v1","service-v4") else "STANDARD_IA") for key,ident,_ in initial]
        second=[(key,ident,"GLACIER" if sc=="STANDARD_IA" else sc) for key,ident,sc in first]
        last=[row for row in second if row[2]=="STANDARD"]
        return scope_begin()+[(200,{},b""),(200,{},b""),ENABLED,LC]+[put_result(i) for i in range(5)]+[(200,XML,version_rows(rows)) for rows in (initial,first,second,last)]+scope_end(rows=[(key,ident) for key,ident,_ in last])
    if name=="plain_null":
        disabled=(200,XML,b"<VersioningConfiguration/>")
        return scope_begin()+[disabled,(200,{},b""),(200,{},b""),ENABLED,LC,(200,XML,version_rows([("testobjfoo","null","STANDARD_IA")]))]+scope_end(rows=[("testobjfoo","null")])
    raise ValueError(name)

@unittest.skipUnless(os.environ.get("OSV_RUN_LIFECYCLE_TRANSITION_TESTS")=="1","enable actual SDK worker tests")
class LifecycleTransitionSdkTest(unittest.TestCase):
    """Separate real SDK phase/wire verification from unprovided multi-class backend lifecycle certification."""
    @classmethod
    def setUpClass(cls):
        """Start common local SDK fixtures with explicit static credentials, without running unrelated tests."""
        from test_s3_sdk import SdkIntegrationTest
        SdkIntegrationTest.setUpClass.__func__(cls)

    def test_default_missing_backend_classes_remains_not_run(self):
        """Run four source cases without configured service classes or acceleration; expect all not_run and no test resource creation."""
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker(target,30)
                try:
                    context=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    result=run_cases(context,CASES)
                    self.assertEqual([row["status"] for row in result],["not_run"]*4)
                finally:worker.close()
                self.assertEqual([row["Name"] for row in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_complete_original_transition_stages_and_null_version_inputs(self):
        """Consume literal version/class documents through actual SDKs and the source scenarios; expect every phase count, unchanged wait factors and genuine version-aware cleanup."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            for case in CASES:
                name=case.name.removeprefix("ceph_lc_transition_");documents=stages(name)
                with self.subTest(language=language,case=name),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,documents,"s3") as (worker,server):
                    directory=Path(temporary);context=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{"storage_classes":["STANDARD","STANDARD_IA","GLACIER"],"lifecycle_debug_interval_seconds":0.001})
                    if case.operations-worker.capabilities:
                        result=run_cases(context,[case])[0];self.assertEqual(result["status"],"not_run");self.assertEqual(server.requests,[]);continue
                    with patch("osv.ceph_lifecycle_transition.wait",wraps=wait) as observed:
                        case.execute(Probe(context))
                    self.assertEqual([item.args[0] for item in observed.call_args_list],[factor*.001 for factor in {"current":[4,1,5],"current_multiple":[5,1,6],"noncurrent":[4,4,6],"plain_null":[4]}[name]])
                    self.assertEqual(len(server.requests),len(documents))
                    rules=[ET.fromstring(row[3]) for row in server.requests if row[0]=="PUT" and "lifecycle" in row[1]][0]
                    transitions=rules.findall(".//{*}Transition")+rules.findall(".//{*}NoncurrentVersionTransition")
                    self.assertEqual([node.findtext("{*}StorageClass") for node in transitions],["STANDARD_IA"] if name=="plain_null" else ["STANDARD_IA","GLACIER"])
                    self.assertEqual([node.findtext("{*}Days") or node.findtext("{*}NoncurrentDays") for node in transitions],{"current":["1","6"],"current_multiple":["1","7"],"noncurrent":["1","5"],"plain_null":["1"]}[name])
                    if name in ("noncurrent","plain_null"):
                        puts=[row for row in server.requests if row[0]=="PUT" and row[3]==b"fooz"]
                        self.assertEqual(len(puts),1)
                        enable=next(i for i,row in enumerate(server.requests) if row[0]=="PUT" and "versioning" in row[1])
                        self.assertLess(server.requests.index(puts[0]),enable)
                    if name=="plain_null":self.assertTrue(any(row[0]=="DELETE" and "versionId=null" in row[1] for row in server.requests))
