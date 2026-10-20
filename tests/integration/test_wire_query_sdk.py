"""Inspect real before-signing query changes without replacing SDK serializers or signing."""
import os
import re
import unittest
from urllib.parse import urlsplit,parse_qsl
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_WIRE_QUERY_TESTS")=="1","build real AWS SDK workers first")
class WireQuerySdkTest(unittest.TestCase):
    """Use independent HTTP replies and the official Python signer to verify actual Java/C++ request queries and signatures."""
    def languages(self):return os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(",")

    def verify_signature(self,record,port):
        from botocore.awsrequest import AWSRequest
        from botocore.auth import S3SigV4Auth
        from botocore.credentials import Credentials
        method,path,original,body=record
        headers={key.lower():value for key,value in original.items()}
        authorization=headers["authorization"]
        signed=re.search(r"SignedHeaders=([^, ]+)",authorization).group(1).split(";")
        selected={name:headers[name] for name in signed}
        request=AWSRequest(method=method,url=f"http://127.0.0.1:{port}"+path,data=body,headers=selected)
        request.context["timestamp"]=headers["x-amz-date"]
        signer=S3SigV4Auth(Credentials("local-test","local-test"),"s3","ap-guangzhou")
        expected=signer.signature(signer.string_to_sign(request,signer.canonical_request(request)),request)
        self.assertEqual(authorization.rsplit("Signature=",1)[1],expected)

    def test_literal_values_flags_removal_signing_and_next_request_isolation(self):
        """Change typed MaxKeys after serialization, add reserved UTF-8 data and an empty-value flag, remove Prefix; expect exact final parameters, official matching signatures and no next-call leakage."""
        xml=b'<ListBucketResult><Name>fixture</Name><IsTruncated>false</IsTruncated></ListBucketResult>'
        for language in self.languages():
            with self.subTest(language=language),fixture_worker(language,[(200,{"Content-Type":"application/xml"},xml)]*2,"s3") as (worker,server):
                self.assertIn("wire_query",worker.capabilities)
                result=worker.call({"op":"list_v1_page","prefix":"must-remove","max_keys":10,"wire_query":{"max-keys":"n/a","marker":"A +/%&=é","flag":True,"prefix":None}})
                self.assertTrue(result["ok"],result)
                self.assertTrue(worker.call({"op":"list_v1_page","prefix":"ordinary","max_keys":7})["ok"])
                params=dict(parse_qsl(urlsplit(server.requests[0][1]).query,keep_blank_values=True))
                self.assertEqual(params,{"max-keys":"n/a","marker":"A +/%&=é","flag":""})
                self.assertNotIn("+",server.requests[0][1]);self.assertIn("%26",server.requests[0][1])
                self.assertEqual(dict(parse_qsl(urlsplit(server.requests[1][1]).query)),{"prefix":"ordinary","max-keys":"7"})
                for row in server.requests:self.verify_signature(row,server.server_port)

    def test_invalid_query_patches_emit_zero_http(self):
        """Reject malformed map/names/types before any HTTP, including false flags and non-string numbers; expect no accidental fallback to an unmodified request."""
        patches=[None,[],"bad",{"": "value"},{"line\nname":"x"},{"x":False},{"x":1},{"x":1.5},{"x":[]},{"x":{}}]
        for language in self.languages():
            with self.subTest(language=language),fixture_worker(language,[],"s3") as (worker,server):
                for patch in patches:
                    row=worker.call({"op":"list_v1_page","wire_query":patch})
                    self.assertFalse(row["ok"],(patch,row))
                self.assertEqual(server.requests,[])

    def test_failed_request_restores_query_scope(self):
        """Return a real SDK error for a patched request then a clean success; expect observed 400 preserved and no query mutation on cleanup/next call."""
        error=b'<Error><Code>InvalidArgument</Code><Message>independent error</Message></Error>'
        xml=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        for language in self.languages():
            with self.subTest(language=language),fixture_worker(language,[(400,{"Content-Type":"application/xml"},error),(200,{"Content-Type":"application/xml"},xml)],"s3") as (worker,server):
                result=worker.call({"op":"list_v1_page","wire_query":{"allow-unordered":True}})
                self.assertFalse(result["ok"],result);self.assertEqual(result["status_code"],400)
                self.assertTrue(worker.call({"op":"list_v1_page"})["ok"])
                self.assertIn("allow-unordered",server.requests[0][1]);self.assertNotIn("allow-unordered",server.requests[1][1])
