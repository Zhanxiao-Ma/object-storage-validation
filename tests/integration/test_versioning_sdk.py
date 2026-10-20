"""Observe actual AWS SDK versioning XML, opaque IDs, historical IO and error headers."""
import os
import hashlib
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs,urlsplit
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_VERSION_TESTS")=="1","enable after building actual AWS SDK workers")
class VersioningSdkTest(unittest.TestCase):
    """Preserve real SDK values and service field absence instead of inferring object versions from requests."""
    def test_configuration_absence_status_and_mfa_xml(self):
        """Consume absent/enabled configuration and issue a suspended update; expect actual field presence and original Status/MfaDelete XML."""
        responses=[(200,{"Content-Type":"application/xml"},b"<VersioningConfiguration/>"),
                   (200,{"Content-Type":"application/xml"},b"<VersioningConfiguration><Status>Enabled</Status><MfaDelete>Disabled</MfaDelete></VersioningConfiguration>"),(200,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,responses,"s3") as (worker,server):
                result=worker.call({"op":"versioning_get"});self.assertTrue(result["ok"],result);self.assertNotIn("versioning_status",result)
                result=worker.call({"op":"versioning_get"});self.assertTrue(result["ok"],result);self.assertEqual(result["versioning_status"],"Enabled");self.assertEqual(result["mfa_delete"],"Disabled")
                result=worker.call({"op":"versioning_put","status":"Suspended","mfa_delete":"Disabled"});self.assertTrue(result["ok"],result)
                document=ET.fromstring(server.requests[2][3]);self.assertEqual({node.tag.rsplit("}",1)[-1]:node.text for node in document},{"Status":"Suspended","MfaDelete":"Disabled"})
                self.assertEqual([row[0] for row in server.requests],["GET","GET","PUT"])

    def test_version_ids_historical_body_and_delete_marker_are_server_values(self):
        """Upload real file bytes and request an opaque version; expect service-returned IDs, whole body, HEAD metadata and actual marker headers, without request echo substitution."""
        historical_etag='"'+hashlib.md5(b"body").hexdigest()+'"'
        responses=[(200,{"ETag":'"'+hashlib.md5(b"real-producer").hexdigest()+'"',"x-amz-version-id":"server-put-id"},b""),
                   (200,{"ETag":historical_etag,"x-amz-version-id":"different-service-id","Content-Length":"4"},b"body"),
                   (200,{"ETag":historical_etag,"x-amz-version-id":"head-service-id","Content-Length":"4"},b""),
                   (204,{"x-amz-version-id":"marker-service-id","x-amz-delete-marker":"true"},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,responses,"s3") as (worker,server):
                directory=Path(temporary);body=directory/"body";body.write_bytes(b"real-producer");output=directory/"output"
                result=worker.call({"op":"put_version","key":"object","body_path":str(body)})
                self.assertTrue(result["ok"],result);self.assertEqual(result["version_id"],"server-put-id");self.assertEqual(server.requests[0][3],b"real-producer")
                requested="v+1/=&"
                for op,expected in (("get_version","different-service-id"),("head_version","head-service-id")):
                    result=worker.call({"op":op,"key":"object","version_id":requested,"output_path":str(output)})
                    self.assertTrue(result["ok"],result);self.assertEqual(result["version_id"],expected);self.assertEqual(result["etag"],historical_etag);self.assertEqual(result["size"],4)
                self.assertEqual(output.read_bytes(),b"body")
                result=worker.call({"op":"delete_version","key":"object","version_id":requested})
                self.assertTrue(result["ok"],result);self.assertTrue(result["delete_marker"]);self.assertEqual(result["version_id"],"marker-service-id")
                for request in server.requests[1:]:self.assertEqual(parse_qs(urlsplit(request[1]).query),{"versionId":[requested]})

    def test_actual_version_list_order_markers_cursors_and_empty_fields(self):
        """Consume independently ordered version/delete-marker XML then an empty page; expect unchanged IDs/order, actual continuation markers and no invented Versions field."""
        document=b'<ListVersionsResult><Name>fixture</Name><KeyMarker>input-key</KeyMarker><VersionIdMarker>input-id</VersionIdMarker><NextKeyMarker>server-next-key</NextKeyMarker><NextVersionIdMarker>server-next-id</NextVersionIdMarker><IsTruncated>true</IsTruncated><Version><Key>server-key</Key><VersionId>server-latest</VersionId><IsLatest>true</IsLatest><Size>4</Size><ETag>"server-etag"</ETag><LastModified>2026-01-01T00:00:00Z</LastModified></Version><Version><Key>server-key</Key><VersionId>server-older</VersionId><IsLatest>false</IsLatest><Size>3</Size><LastModified>2025-01-01T00:00:00Z</LastModified></Version><DeleteMarker><Key>marker-key</Key><VersionId>server-marker</VersionId><IsLatest>true</IsLatest><LastModified>2026-01-01T00:00:00Z</LastModified></DeleteMarker></ListVersionsResult>'
        responses=[(200,{"Content-Type":"application/xml"},document),(200,{"Content-Type":"application/xml"},b"<ListVersionsResult><IsTruncated>false</IsTruncated></ListVersionsResult>")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,responses,"s3") as (worker,server):
                result=worker.call({"op":"versions_list","key_marker":"input-key","version_id_marker":"input-id","max_keys":2})
                self.assertTrue(result["ok"],result);self.assertTrue(result["truncated"])
                self.assertEqual([row["version_id"] for row in result["versions"]],["server-latest","server-older"])
                self.assertEqual([row["size"] for row in result["versions"]],[4,3]);self.assertEqual(result["versions"][0]["etag"],'"server-etag"')
                self.assertEqual(result["delete_markers"][0]["version_id"],"server-marker");self.assertEqual(set(result["fields"]),{"Versions","DeleteMarkers"})
                self.assertEqual((result["next_key_marker"],result["next_version_id_marker"]),("server-next-key","server-next-id"))
                query=parse_qs(urlsplit(server.requests[0][1]).query)
                self.assertEqual(query["key-marker"],["input-key"]);self.assertEqual(query["version-id-marker"],["input-id"]);self.assertEqual(query["max-keys"],["2"])
                result=worker.call({"op":"versions_list"});self.assertTrue(result["ok"],result);self.assertFalse(result["truncated"])
                self.assertEqual(result["versions"],[]);self.assertEqual(result["delete_markers"],[]);self.assertEqual(result["fields"],[])

    def test_missing_head_preserves_actual_delete_marker_error_header(self):
        """Return a real bodyless HEAD 404 with a marker header; expect the SDK failure and original error header to reach the case unchanged."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(404,{"x-amz-delete-marker":"true","x-amz-version-id":"marker-from-error"},b"")],"s3") as (worker,server):
                result=worker.call({"op":"head_version","key":"object"})
                self.assertFalse(result["ok"],result);self.assertEqual(result["status_code"],404)
                marker=result["http_headers"]["x-amz-delete-marker"]
                self.assertEqual(marker[0] if isinstance(marker,list) else marker,"true")
