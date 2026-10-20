"""Inspect actual AWS Java/C++ tagging requests against independent fixed service responses."""
import os
import unittest
from urllib.parse import urlsplit,parse_qs
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_TAG_TESTS")=="1","enable after building AWS SDK workers")
class TagSdkTest(unittest.TestCase):
    """Verify tag serialization, successful response observation and unmodified service error codes."""
    def test_actual_tag_requests_and_response_observations(self):
        """Send unordered/special-character tags through actual SDKs; expect exact XML values, raw response order/status and InvalidTag."""
        tags=[{"key":"z & key","value":"<&value>"},{"key":"a","value":""}]
        document=b"<Tagging><TagSet><Tag><Key>z &amp; key</Key><Value>&lt;&amp;value&gt;</Value></Tag><Tag><Key>a</Key><Value></Value></Tag></TagSet></Tagging>"
        error=b"<Error><Code>InvalidTag</Code><Message>independent rejection</Message><RequestId>fixture</RequestId></Error>"
        fixtures=[(200,{},b""),(200,{"Content-Type":"application/xml"},document),(204,{},b""),(400,{"Content-Type":"application/xml"},error)]
        selected=os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(",")
        for language in selected:
            with self.subTest(language=language),fixture_worker(language,fixtures,"s3") as (worker,server):
                result=worker.call({"op":"tags_put","key":"object","tags":tags})
                self.assertTrue(result["ok"],result);self.assertEqual(result["status_code"],200)
                result=worker.call({"op":"tags_get","key":"object"})
                self.assertTrue(result["ok"],result);self.assertEqual(result["tags"],tags)
                result=worker.call({"op":"tags_delete","key":"object"})
                self.assertTrue(result["ok"],result);self.assertEqual(result["status_code"],204)
                excessive=[{"key":str(i),"value":str(i)} for i in range(11)]
                result=worker.call({"op":"tags_put","key":"object","tags":excessive})
                self.assertFalse(result["ok"],result);self.assertEqual(result["error_code"],"InvalidTag");self.assertEqual(result["status_code"],400)
                for index,expected in ((0,tags),(3,excessive)):
                    root=ET.fromstring(server.requests[index][3])
                    pairs=[{"key":next((n.text or "" for n in tag if n.tag.rsplit("}",1)[-1]=="Key")),"value":next((n.text or "" for n in tag if n.tag.rsplit("}",1)[-1]=="Value"))} for tag in root.iter() if tag.tag.rsplit("}",1)[-1]=="Tag"]
                    self.assertEqual(pairs,expected)
                self.assertEqual([r[0] for r in server.requests],["PUT","GET","DELETE","PUT"])
                for row in server.requests:self.assertIn("tagging",parse_qs(urlsplit(row[1]).query,keep_blank_values=True))
