"""Inspect genuine SDK list V1/V2 field presence without inferring response fields from request options."""
import os
import unittest
from urllib.parse import parse_qs,urlsplit
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_LIST_TESTS")=="1","enable after building real AWS SDK workers")
class ObservedListSdkTest(unittest.TestCase):
    """Expose actual decoded markers/count/owner values and raw field presence through the typed SDK path."""
    def test_missing_empty_and_unexpected_fields_remain_observable(self):
        """Supply independent server echoes, empty Delimiter and unexpected Owner; expect literal observations plus exact V1/V2 query serialization."""
        first=b"<ListBucketResult><Name>server</Name><Prefix>server-prefix/</Prefix><MaxKeys>17</MaxKeys><IsTruncated>true</IsTruncated><Marker>server-marker</Marker><NextMarker>server-next</NextMarker><KeyCount>2</KeyCount><ContinuationToken>server-token</ContinuationToken><NextContinuationToken>server-next-token</NextContinuationToken><StartAfter>server-start</StartAfter><Contents><Key>server-key</Key><Size>4</Size></Contents></ListBucketResult>"
        second=b"<ListBucketResult><Name>server</Name><Prefix></Prefix><Delimiter></Delimiter><MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated><KeyCount>1</KeyCount><Contents><Key>server-key</Key><Size>4</Size><Owner><ID>server-owner</ID></Owner></Contents></ListBucketResult>"
        error=b"<Error><Code>InvalidArgument</Code><Message>independent error after XML observation</Message></Error>"
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            for op in ("list_v1_page","list_v2_observed"):
                with self.subTest(language=language,op=op),fixture_worker(language,[(200,{"Content-Type":"application/xml"},first),(200,{"Content-Type":"application/xml"},second),(400,{"Content-Type":"application/xml"},error)],"s3") as (worker,server):
                    request={"op":op,"prefix":"request-prefix/","max_keys":3}
                    if op=="list_v1_page":request["marker"]="request-marker"
                    else:request.update(start_after="request-start",token="request-token",fetch_owner=False)
                    result=worker.call(request);self.assertTrue(result["ok"],result)
                    self.assertEqual(result["keys"],["server-key"]);self.assertEqual(result["prefix"],"server-prefix/");self.assertEqual(result["max_keys"],17)
                    self.assertNotIn("Delimiter",result["fields"]);self.assertIsNone(result["delimiter"]);self.assertNotIn("owner",result["objects"][0])
                    query=parse_qs(urlsplit(server.requests[0][1]).query,keep_blank_values=True)
                    self.assertEqual(query["prefix"],["request-prefix/"]);self.assertEqual(query["max-keys"],["3"])
                    if op=="list_v1_page":
                        self.assertEqual(result["marker"],"server-marker");self.assertEqual(result["next_marker"],"server-next");self.assertEqual(query["marker"],["request-marker"]);self.assertNotIn("list-type",query)
                    else:
                        self.assertEqual(result["token"],"server-token");self.assertEqual(result["next_token"],"server-next-token");self.assertEqual(result["start_after"],"server-start");self.assertEqual(result["key_count"],2)
                        self.assertEqual(query["list-type"],["2"]);self.assertEqual(query["continuation-token"],["request-token"]);self.assertEqual(query["start-after"],["request-start"]);self.assertEqual(query["fetch-owner"],["0" if language=="cpp" else "false"])
                    result=worker.call({"op":op,"delimiter":"",**({"fetch_owner":False} if op=="list_v2_observed" else {})})
                    self.assertTrue(result["ok"],result);self.assertIn("Delimiter",result["fields"]);self.assertEqual(result["delimiter"],"");self.assertEqual(result["objects"][0]["owner"]["id"],"server-owner")
                    self.assertIsNone(result["next_marker" if op=="list_v1_page" else "next_token"])
                    result=worker.call({"op":op,"max_keys":-1});self.assertFalse(result["ok"],result);self.assertEqual(result["status_code"],400);self.assertEqual(result["error_code"],"InvalidArgument")
                    self.assertEqual(len(server.requests),3)

    def test_encoded_keys_remain_authoritative_sdk_values(self):
        """Supply encoded XML values through each typed SDK; expect Java decoding and C++ encoded values without adapter re-encoding."""
        document=b"<ListBucketResult><Name>fixture</Name><Prefix></Prefix><Delimiter>/</Delimiter><EncodingType>url</EncodingType><MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated><KeyCount>4</KeyCount><Contents><Key>asdf%2Bb</Key><Size>1</Size></Contents><CommonPrefixes><Prefix>foo%2B1/</Prefix></CommonPrefixes><CommonPrefixes><Prefix>foo/</Prefix></CommonPrefixes><CommonPrefixes><Prefix>quux%20ab/</Prefix></CommonPrefixes></ListBucketResult>"
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            for op in ("list_v1_page","list_v2_observed"):
                with self.subTest(language=language,op=op),fixture_worker(language,[(200,{"Content-Type":"application/xml"},document)],"s3") as (worker,server):
                    result=worker.call({"op":op,"encoding_type":"url","delimiter":"/"})
                    self.assertTrue(result["ok"],result)
                    self.assertEqual(result["keys"],["asdf+b"] if language=="java" else ["asdf%2Bb"])
                    self.assertEqual(result["common_prefixes"],["foo+1/","foo/","quux ab/"] if language=="java" else ["foo%2B1/","foo/","quux%20ab/"])
                    query=parse_qs(urlsplit(server.requests[0][1]).query)
                    self.assertEqual(query["encoding-type"],["url"])
