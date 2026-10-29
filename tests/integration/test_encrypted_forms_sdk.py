"""Inspect original encrypted POST forms and actual SDK consumers without pretending raw HTTP is SDK performance."""
import base64
from email.parser import BytesParser
from email.policy import default
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import unittest
from osv.ceph_encrypted_forms import encrypted_form
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_ENCRYPTED_FORM_TESTS")=="1","requires built real AWS SDK workers")
class EncryptedFormsSdkTest(unittest.TestCase):
    """Bind independently specified form policy/header oracles to real HTTP and genuine Java/C++ SDK readback."""
    def test_original_four_forms_and_sdk_readback(self):
        """Send all four real production forms; expect scalar fields without filenames, ordered HMAC/encryption inputs, typed default configuration, actual GET bytes/headers and complete cleanup."""
        customer={"x-amz-server-side-encryption-customer-algorithm":"AES256","x-amz-server-side-encryption-customer-key":"pO3upElrwuEXSoFwCfnZPdSsmt/xWeFa0N9KgDijwVs=","x-amz-server-side-encryption-customer-key-md5":"DWygnHRtgiJ77HCm+1rvHw=="}
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            for mode in ("customer","kms","s3_default","kms_default"):
                headers={"Content-Type":"text/plain"}
                if mode=="s3_default":headers["x-amz-server-side-encryption"]="AES256"
                if mode=="kms_default":headers.update({"x-amz-server-side-encryption":"aws:kms","x-amz-server-side-encryption-aws-kms-key-id":"independent-kms"})
                fixtures=[(404,{},b""),(200,{},b"")]+([(200,{},b"")] if mode.endswith("_default") else [])+[(204,{},b""),(200,headers,b"bar"),(200,{},b""),(204,{},b""),(200,{"Content-Type":"application/xml"},b"<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>"),(204,{},b""),(404,{},b"")]
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                    directory=Path(temporary);workload={"ceph_http":{"endpoint":worker.info["endpoint"],"addressing_style":"path"},"kms_key_id":"independent-kms"}
                    result=encrypted_form(Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",workload)),mode)
                    self.assertFalse(result["sdk_performance_sample"]);self.assertEqual(result["request_transport"],"raw_http")
                    post=next(x for x in server.requests if x[0]=="POST");http={k.lower():v for k,v in post[2].items()}
                    self.assertNotIn("authorization",http)
                    document=BytesParser(policy=default).parsebytes(("Content-Type: "+http["content-type"]+"\r\n\r\n").encode()+post[3]);parts=list(document.iter_parts());names=[x.get_param("name",header="content-disposition") for x in parts];fields={n:x.get_payload(decode=True) for n,x in zip(names,parts)}
                    enc={k.removesuffix("-md5")+"-MD5" if k.endswith("-md5") else k:v for k,v in customer.items()} if mode=="customer" else {"x-amz-server-side-encryption":"aws:kms","x-amz-server-side-encryption-aws-kms-key-id":"independent-kms"} if mode=="kms" else {}
                    self.assertEqual(names,["key","AWSAccessKeyId","acl","signature","policy","Content-Type",*enc,"file"])
                    self.assertEqual([x.get_filename() for x in parts],[None]*(len(names)-1)+["file"])
                    for k,v in {"key":"foo.txt","AWSAccessKeyId":"local-test","acl":"private","Content-Type":"text/plain","file":"bar",**enc}.items():self.assertEqual(fields[k],v.encode())
                    self.assertEqual(fields["signature"],base64.b64encode(hmac.new(b"local-test",fields["policy"],hashlib.sha1).digest()))
                    conditions=json.loads(base64.b64decode(fields["policy"]))["conditions"]
                    self.assertEqual(conditions,[{"bucket":post[1].strip("/")},["starts-with","$key","foo"],{"acl":"private"},["starts-with","$Content-Type","text/plain"],*[["starts-with","$"+k,""] for k in (enc or {"x-amz-server-side-encryption":""})],["content-length-range",0,1024]])
                    get=next(x for x in server.requests if x[0]=="GET" and "/foo.txt" in x[1]);actual={k.lower():v for k,v in get[2].items()}
                    self.assertIn("authorization",actual)
                    for k,v in customer.items():self.assertEqual(actual.get(k),v if mode=="customer" else None)
                    if mode.endswith("_default"):
                        configuration=next(x for x in server.requests if "encryption" in x[1]);self.assertIn(b"aws:kms" if mode=="kms_default" else b"AES256",configuration[3])
                        if mode=="kms_default":self.assertIn(b"independent-kms",configuration[3])
                    self.assertEqual(len(server.requests),len(fixtures));self.assertEqual(server.requests[-1][0],"HEAD")

    def test_missing_source_inputs_precede_any_bucket_mutation(self):
        """Omit KMS or raw transport configuration; expect explicit prerequisite before any actual HTTP, preserving absent source settings."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3") as (worker,server):
                directory=Path(temporary);probe=Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{}))
                for mode in ("customer","kms","s3_default","kms_default"):
                    with self.assertRaises(CaseNotRun):encrypted_form(probe,mode)
                self.assertEqual(server.requests,[])
