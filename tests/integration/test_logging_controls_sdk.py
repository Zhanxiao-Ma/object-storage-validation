"""Real SDK fixtures for standard logging configuration and source-policy permission mutations."""
import json
import os
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.worker import Worker
from osv.ceph_logging_controls import CASES,remove,permissions,owner,roundtrip,errors
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_LOGGING_CONTROL_TESTS")=="1","enable after standard logging API model bridge build")
class LoggingControlsSdkTest(unittest.TestCase):
    """Preserve complete source oracles; assert actual native SDK serialization, identity selection and logging-disabled cleanup."""
    @classmethod
    def setUpClass(cls):
        """Reuse explicitly local isolated service and require actual Java/C++ binaries."""
        import test_s3_sdk
        test_s3_sdk.SdkIntegrationTest.setUpClass.__func__(cls)

    def test_logging_disable_source_and_cleanup_observe_real_sdk_absence(self):
        """Feed a real empty BucketLoggingStatus response into the source remove case; expect status/absence oracle retained, exact enable/disable requests and all sources disabled before deletion."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        fixtures=[(404,{},b""),(200,{},b""),(404,{},b""),(200,{},b""),(204,{},b""),(200,{},b""),(200,{},b""),(200,{},b'<BucketLoggingStatus/>')]+[(200,{},b"")]*2+[(204,{},b"")]*2+[(200,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]*2
        names=[uuid.UUID(int=1),uuid.UUID(int=2)]
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3") as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{"identities":{"main":{"canonical_id":"independent-main-id","user_id":"independent-user-id"}}})
                with patch("osv.bucket_scope.uuid.uuid4",side_effect=names):
                    remove(Probe(ctx))
                self.assertEqual(len(server.requests),20)
                enabled=ET.fromstring(server.requests[5][3]);fields={n.tag.rsplit("}",1)[-1]:n.text for n in enabled.iter()}
                self.assertEqual(fields["TargetBucket"],"osv-"+names[1].hex);self.assertEqual(fields["TargetPrefix"],"log/")
                for index in (6,8,9):
                    self.assertNotIn("LoggingEnabled",server.requests[index][3].decode())
                    if target["language"]=="cpp":self.assertEqual(server.requests[index][3],b"")
                self.assertTrue(all(server.requests[index][0]=="DELETE" and "?policy" in server.requests[index][1] for index in (10,11)))

    def test_logging_permission_mutations_retain_service_principal_and_repr_bug(self):
        """Replay every original permission rejection through actual models; expect original seven policy documents, strict HTTP 204, genuine service errors and preserved Boto resource repr in final source ARN."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';denied=(403,{},b'<Error><Code>AccessDenied</Code></Error>')
        fixtures=[(404,{},b""),(200,{},b""),(404,{},b""),(200,{},b""),denied]+[(204,{},b""),denied]*7+[(200,{},b"")]*2+[(204,{},b"")]*2+[(200,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]*2
        names=[uuid.UUID(int=3),uuid.UUID(int=4)]
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3") as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{"identities":{"main":{"canonical_id":"independent-main-id","user_id":"independent-user-id"}}})
                with patch("osv.bucket_scope.uuid.uuid4",side_effect=names):permissions(Probe(ctx))
                self.assertEqual(len(server.requests),31)
                policies=[json.loads(r[3])["Statement"][0] for r in server.requests if r[0]=="PUT" and "?policy" in r[1]]
                self.assertEqual(len(policies),7)
                self.assertEqual(policies[0]["Principal"],{"AWS":"*"});self.assertEqual(policies[1]["Principal"],{"Service":"logging.s3.amazonaws.comkaboom"});self.assertEqual(policies[2]["Action"],["s3:GetObject"])
                self.assertTrue(policies[3]["Resource"].endswith("/kaboom"));self.assertEqual(policies[4]["Condition"]["ArnLike"]["aws:SourceArn"],"arn:aws:s3:::kaboom")
                for policy in policies[-2:]:self.assertEqual(policy["Condition"]["ArnLike"]["aws:SourceArn"],"arn:aws:s3:::s3.Bucket(name='osv-"+names[0].hex+"')")
                self.assertEqual(policies[-2]["Condition"]["StringEquals"]["aws:SourceAccount"],"kaboom");self.assertEqual(policies[-1]["Condition"]["StringEquals"]["aws:SourceAccount"],"kaboom$independent-user-id")

    def test_logging_owner_preserves_resource_less_policy_and_alt_denial(self):
        """Replay owner's successful logging and alternate enable/disable errors; expect source policy to keep missing Resource and exact alternate SDK credentials on both forbidden calls."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';denied=(403,{},b'<Error><Code>AccessDenied</Code></Error>')
        fixtures=[(404,{},b""),(200,{},b""),(404,{},b""),(200,{},b""),(204,{},b""),(204,{},b""),denied,(200,{},b""),denied]+[(200,{},b"")]*2+[(204,{},b"")]*2+[(200,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]*2
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{"identities":{"main":{"canonical_id":"independent-main-id","user_id":"independent-user-id"}}})
                owner(Probe(ctx));self.assertEqual(len(server.requests),21)
                item=json.loads(server.requests[5][3])["Statement"][0];self.assertNotIn("Resource",item);self.assertEqual(item["Action"],["s3:PutBucketLogging"])
                for index in (6,8):self.assertIn("Credential=alternate-key/",{k.lower():v for k,v in server.requests[index][2].items()}["authorization"])

    def test_logging_key_format_gate_and_full_cpp_configuration_vectors(self):
        """Exercise exact source simple/partitioned/grant vectors through the real C++ model; expect old Java to report both whole-source cases unsupported without network calls, preserving all required key-format branches."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';names=[uuid.UUID(int=1),uuid.UUID(int=2)]
        def configured(partition=False):
            key=b'<PartitionedPrefix><PartitionDateSource>DeliveryTime</PartitionDateSource></PartitionedPrefix>' if partition else b'<SimplePrefix/>'
            return b'<BucketLoggingStatus><LoggingEnabled><TargetBucket>osv-'+names[1].hex.encode()+b'</TargetBucket><TargetPrefix>log/</TargetPrefix><TargetObjectKeyFormat>'+key+b'</TargetObjectKeyFormat></LoggingEnabled></BucketLoggingStatus>'
        fixtures=[(404,{},b""),(200,{},b""),(404,{},b""),(200,{},b""),(204,{},b"")]
        for partition in (False,False,True,False):fixtures+=[(200,{},b""),(200,{},configured(partition))]
        fixtures+=[(200,{},b"")]*2+[(204,{},b"")]*2+[(200,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]*2
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures if target["language"]=="cpp" else [],"s3") as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{"identities":{"main":{"canonical_id":"independent-main-id","user_id":"independent-user-id","display_name":"independent-main-name"}}})
                if target["language"]=="java":
                    self.assertNotIn("logging_target_key_format",worker.capabilities)
                    rows=run_cases(ctx,tuple(c for c in CASES if c.name in ("ceph_logging_controls_roundtrip","ceph_logging_controls_errors")))
                    self.assertEqual([r["status"] for r in rows],["not_run","not_run"]);self.assertTrue(all("logging_target_key_format" in r["missing_operations"] for r in rows));self.assertEqual(server.requests,[]);continue
                self.assertIn("logging_target_key_format",worker.capabilities)
                with patch("osv.bucket_scope.uuid.uuid4",side_effect=names):roundtrip(Probe(ctx))
                self.assertEqual(len(server.requests),25)
                bodies=[ET.fromstring(server.requests[index][3]) for index in (5,7,9,11)]
                self.assertNotIn("TargetObjectKeyFormat",server.requests[5][3].decode());self.assertIn("SimplePrefix",server.requests[7][3].decode())
                date=[n.text for n in bodies[2].iter() if n.tag.rsplit("}",1)[-1]=="PartitionDateSource"];self.assertEqual(date,["DeliveryTime"])
                grant={n.tag.rsplit("}",1)[-1]:n.text for n in bodies[3].iter()}
                self.assertEqual(json.loads(server.requests[4][3])["Statement"][0]["Condition"]["StringEquals"]["aws:SourceAccount"],"independent-user-id");self.assertEqual(grant["ID"],"independent-main-id");self.assertEqual(grant["DisplayName"],"independent-main-name");self.assertEqual(grant["Permission"],"FULL_CONTROL")

    def test_logging_error_vectors_preserve_exact_service_codes_and_partition_input(self):
        """Replay all standard source rejection codes through the capable C++ SDK; expect malformed literal partition value, same/nested/encrypted/requester-pays targets and all logging disabled before teardown."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        def rejected(code,status=400):return (status,{},("<Error><Code>"+code+"</Code></Error>").encode())
        fixtures=[(404,{},b""),(200,{},b"")]*4+[(404,{},b""),rejected("NoSuchBucket",404),rejected("NoSuchKey",404),(204,{},b""),(200,{},b""),(204,{},b""),rejected("InvalidArgument"),(204,{},b""),rejected("MalformedXML"),(204,{},b""),rejected("InvalidArgument"),(200,{},b""),rejected("InvalidArgument"),(204,{},b""),(200,{},b""),rejected("InvalidArgument")]+[(200,{},b"")]*4+[(204,{},b"")]*4+[(200,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]*4
        for target in self.targets:
            if target["language"]!="cpp":continue
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3") as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{"identities":{"main":{"canonical_id":"independent-main-id","user_id":"independent-user-id"}}})
                errors(Probe(ctx));self.assertEqual(len(server.requests),48)
                date=[n.text for n in ET.fromstring(server.requests[16][3]).iter() if n.tag.rsplit("}",1)[-1]=="PartitionDateSource"];self.assertEqual(date,["kaboom"])
                payer=[n.text for n in ET.fromstring(server.requests[22][3]).iter() if n.tag.rsplit("}",1)[-1]=="Payer"];self.assertEqual(payer,["Requester"])
                for index in range(24,28):self.assertNotIn("LoggingEnabled",server.requests[index][3].decode())

    def test_default_and_configured_moto_logging_keep_prerequisites_and_diagnostics(self):
        """Run actual source cases with/without independent main identity metadata; expect explicit default prerequisites, old-Java key-format gate and preserved local service policy/target errors without bucket remnants."""
        for configured in (False,True):
            overrides={"OSV_ALT_ACCESS_KEY":"alternate-key" if configured else "","OSV_ALT_SECRET_KEY":"alternate-secret" if configured else ""}
            with patch.dict(os.environ,overrides):
                for target in self.targets:
                    with self.subTest(language=target["language"],configured=configured),tempfile.TemporaryDirectory() as temporary:
                        d=Path(temporary);worker=Worker(target,30)
                        try:
                            workload={"identities":{"main":{"canonical_id":"independent-main-id","user_id":"independent-user-id","display_name":"independent-main-name"}}} if configured else {}
                            rows=run_cases(Context(worker,"unused/",d,b"independent",d/"body",d/"output",workload),CASES)
                            unsupported={"ceph_logging_controls_roundtrip","ceph_logging_controls_errors"} if target["language"]=="java" else set()
                            not_run=unsupported if configured else {c.name for c in CASES}
                            self.assertEqual({r["case"] for r in rows if r["status"]=="not_run"},not_run)
                            self.assertEqual({r["case"] for r in rows if r["status"]=="fail"},{c.name for c in CASES}-not_run)
                            self.assertFalse(any(r["status"]=="pass" for r in rows));self.assertEqual(len(rows),5)
                            self.assertEqual([b["Name"] for b in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                        finally:worker.close()
