"""Verify real SDK bulk deletion preserves object-level errors and sends the complete caller request."""
import os
import unittest
from urllib.parse import parse_qs,urlsplit
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_DELETE_TESTS")=="1","enable after building actual AWS SDK workers")
class DeleteManySdkTest(unittest.TestCase):
    """Keep actual SDK serialization, response acknowledgments and per-object errors authoritative."""
    def test_partial_result_versions_and_quiet_are_actual_sdk_data(self):
        """Send special/versioned identifiers and consume independent mixed results; expect preserved errors/markers and actual quiet XML."""
        document=b"<DeleteResult><Deleted><Key>server-delete</Key><VersionId>server-version</VersionId><DeleteMarker>true</DeleteMarker><DeleteMarkerVersionId>server-marker</DeleteMarkerVersionId></Deleted><Error><Key>server-error</Key><VersionId>error-version</VersionId><Code>AccessDenied</Code><Message>denied &lt;field&gt;</Message></Error></DeleteResult>"
        identifiers=[{"key":"first <&?+","version_id":"v+1/=&"},{"key":"second"}]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{"Content-Type":"application/xml"},document),(200,{"Content-Type":"application/xml"},b"<DeleteResult/>")],"s3") as (worker,server):
                result=worker.call({"op":"delete_many_versions","objects":identifiers})
                self.assertTrue(result["ok"],result);self.assertEqual(result["status_code"],200);self.assertTrue(result["errors_present"])
                self.assertEqual(result["deleted"],[{"key":"server-delete","version_id":"server-version","delete_marker":True,"delete_marker_version_id":"server-marker"}])
                self.assertEqual(result["errors"],[{"key":"server-error","version_id":"error-version","code":"AccessDenied","message":"denied <field>"}])
                root=ET.fromstring(server.requests[0][3]);objects=[]
                for node in root:
                    if node.tag.rsplit("}",1)[-1]=="Object":objects.append({child.tag.rsplit("}",1)[-1]:child.text for child in node})
                self.assertEqual(objects,[{"Key":identifiers[0]["key"],"VersionId":"v+1/=&"},{"Key":"second"}])
                result=worker.call({"op":"delete_many","objects":[{"key":"quiet-object"}],"quiet":True})
                self.assertTrue(result["ok"],result);self.assertFalse(result["errors_present"]);self.assertEqual(result["deleted"],[])
                root=ET.fromstring(server.requests[1][3]);quiet=next(node.text for node in root if node.tag.rsplit("}",1)[-1]=="Quiet")
                self.assertEqual(quiet,"true")
                self.assertEqual([row[0] for row in server.requests],["POST","POST"])

    def test_1001_identifiers_reach_service_as_one_request(self):
        """Pass 1001 well-typed objects to actual SDKs; expect one HTTP request containing all items and the supplied real 400 rejection."""
        document=b"<Error><Code>MalformedXML</Code><Message>independent single-request limit rejection</Message></Error>"
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(400,{"Content-Type":"application/xml"},document)],"s3") as (worker,server):
                result=worker.call({"op":"delete_many","objects":[{"key":"key-"+str(i)} for i in range(1001)]})
                self.assertFalse(result["ok"],result);self.assertEqual(result["error_kind"],"sdk_error");self.assertEqual(result["status_code"],400);self.assertEqual(result["error_code"],"MalformedXML")
                self.assertEqual(len(server.requests),1)
                root=ET.fromstring(server.requests[0][3]);items=[node for node in root if node.tag.rsplit("}",1)[-1]=="Object"]
                self.assertEqual(len(items),1001)
                self.assertIn("delete",parse_qs(urlsplit(server.requests[0][1]).query,keep_blank_values=True))
