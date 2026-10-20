"""Validate typed ACL requests, genuine creation headers and real source producer/consumer paths."""
import os
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker


def elements(document,name):
    return [node for node in ET.fromstring(document).iter() if node.tag.rsplit("}",1)[-1]==name]

def acl_document(permission="FULL_CONTROL",owner="actual-service-owner",grantee="known-main-id",display="known-main-name"):
    return (f'<AccessControlPolicy><Owner><ID>{owner}</ID><DisplayName>actual-service-owner-name</DisplayName></Owner><AccessControlList><Grant><Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="CanonicalUser"><ID>{grantee}</ID><DisplayName>{display}</DisplayName></Grantee><Permission>{permission}</Permission></Grant></AccessControlList></AccessControlPolicy>').encode()

@unittest.skipUnless(os.environ.get("OSV_RUN_ACL_TESTS")=="1","enable after building actual Java/C++ AWS SDK workers")
class AclSdkTest(unittest.TestCase):
    """Use independent wire documents to test SDK models without implementing ACL storage or authorization rules."""
    def test_typed_acl_nested_models_preserve_input_and_service_fields(self):
        """Send nested canonical/group grants and consume independent server ACLs; expect literal model XML, real xsi:type, keys and decoded Owner/Grants values."""
        policy={"Owner":{"ID":"request-owner","DisplayName":"request-owner-name"},"Grants":[{"Grantee":{"Type":"Group","URI":"http://acs.amazonaws.com/groups/global/AllUsers"},"Permission":"READ"},{"Grantee":{"Type":"CanonicalUser","ID":"request-grantee","DisplayName":"request-grantee-name"},"Permission":"WRITE_ACP"}]}
        response=acl_document("READ_ACP",owner="independent-owner",grantee="independent-grantee",display="independent-display")
        fixtures=[(200,{},b""),(200,{"Content-Type":"application/xml"},response)]*2
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,fixtures,"s3") as (worker,server):
                for kind in ("Bucket","Object"):
                    options={"Bucket":"fixture","AccessControlPolicy":policy}
                    if kind=="Object":options["Key"]="folder/key"
                    result=worker.call({"op":"api.Put"+kind+"Acl","input":options});self.assertTrue(result["ok"],result)
                    options.pop("AccessControlPolicy")
                    result=worker.call({"op":"api.Get"+kind+"Acl","input":options});self.assertTrue(result["ok"],result)
                    self.assertEqual(result["data"]["Owner"],{"ID":"independent-owner","DisplayName":"actual-service-owner-name"})
                    self.assertEqual(result["data"]["Grants"],[{"Grantee":{"Type":"CanonicalUser","ID":"independent-grantee","DisplayName":"independent-display"},"Permission":"READ_ACP"}])
                for request in server.requests[::2]:
                    self.assertEqual([node.text for node in elements(request[3],"Permission")],["READ","WRITE_ACP"])
                    self.assertEqual(ET.fromstring(request[3]).find(".//{*}Owner/{*}ID").text,"request-owner")
                    self.assertEqual(ET.fromstring(request[3]).find(".//{*}Grantee/{*}ID").text,"request-grantee")
                    grantees=elements(request[3],"Grantee")
                    self.assertEqual([node.attrib["{http://www.w3.org/2001/XMLSchema-instance}type"] for node in grantees],["Group","CanonicalUser"])

    def test_creation_and_object_acl_are_actual_signed_headers(self):
        """Create with public-read-write and upload literal bar with authenticated-read; expect genuine x-amz-acl headers, signed values and original object body."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[(200,{},b""),(200,{"ETag":'"37b51d194a7513e45b56f6524f2d51f2"'},b"")],"s3") as (worker,server):
                body=Path(temporary)/"body";body.write_bytes(b"bar")
                result=worker.call({"op":"bucket_create_acl","bucket":"fixture","acl":"public-read-write"});self.assertTrue(result["ok"],result)
                result=worker.call({"op":"put_acl","key":"foo","acl":"authenticated-read","body_path":str(body)});self.assertTrue(result["ok"],result)
                self.assertEqual(server.requests[1][3],b"bar")
                for expected,request in zip(("public-read-write","authenticated-read"),server.requests):
                    headers={key.lower():value for key,value in request[2].items()};self.assertEqual(headers["x-amz-acl"],expected);self.assertIn("x-amz-acl",headers["authorization"])

    def test_source_permission_update_consumes_observed_owner_and_grants(self):
        """Execute the real translated READ case; expect actual service Owner to enter the SDK PUT, original grantee fields retained and only the source permission changed."""
        from osv.ceph_acl import change_permission
        from osv.scenarios import Context
        from osv.suites import Probe
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        fixtures=[(404,{},b""),(200,{},b""),(200,{},b""),(200,{"Content-Type":"application/xml"},acl_document()),(200,{},b""),
                  (200,{"Content-Type":"application/xml"},acl_document("READ")),(200,{},b""),(204,{},b""),
                  (200,{"Content-Type":"application/xml"},empty),(204,{},b""),(404,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                directory=Path(temporary);ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{"identities":{"main":{"canonical_id":"known-main-id","display_name":"known-main-name"}}})
                change_permission(Probe(ctx),"READ")
                self.assertEqual(len(server.requests),11)
                self.assertEqual(ET.fromstring(server.requests[4][3]).find(".//{*}Owner/{*}ID").text,"actual-service-owner")
                self.assertEqual(ET.fromstring(server.requests[4][3]).find(".//{*}Grantee/{*}ID").text,"known-main-id")
                self.assertEqual([node.text for node in elements(server.requests[4][3],"Permission")],["READ"])
                self.assertEqual(ET.fromstring(server.requests[4][3]).find(".//{*}Owner/{*}DisplayName").text,"actual-service-owner-name")
                self.assertEqual(ET.fromstring(server.requests[4][3]).find(".//{*}Grantee/{*}DisplayName").text,"known-main-name")
