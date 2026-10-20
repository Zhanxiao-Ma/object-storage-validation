"""Verify generated/model-driven bridges through actual SDK serialization and deserialization."""
import os
import unittest
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker

def tree(document):
    """Read independent HTTP XML observations; values remain raw strings rather than recreating SDK model logic."""
    return ET.fromstring(document)

def named(root,name):
    return [node for node in root.iter() if node.tag.rsplit("}",1)[-1]==name]

@unittest.skipUnless(os.environ.get("OSV_RUN_API_MODEL_TESTS")=="1","enable after building both actual AWS SDK workers")
class ApiModelsSdkTest(unittest.TestCase):
    """Validate real nested SDK builders/getters with independent wire and service expectations."""
    def test_nested_cors_is_real_wire_and_service_values_are_not_echoed(self):
        """Send nested list/int configuration and consume a different server page; expect exact XML inputs and actual decoded service rules."""
        configuration={"CORSRules":[{"ID":"original-rule","AllowedMethods":["GET","PUT"],"AllowedOrigins":["*.get","*.put"],"AllowedHeaders":["X-Original"],"MaxAgeSeconds":17}]}
        response=b'<CORSConfiguration><CORSRule><ID>server-rule</ID><AllowedMethod>HEAD</AllowedMethod><AllowedOrigin>https://actual.example</AllowedOrigin><MaxAgeSeconds>23</MaxAgeSeconds></CORSRule></CORSConfiguration>'
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{},b""),(200,{"Content-Type":"application/xml"},response)],"s3") as (worker,server):
                result=worker.call({"op":"api.PutBucketCors","input":{"Bucket":"fixture","CORSConfiguration":configuration}})
                self.assertTrue(result["ok"],result)
                document=tree(server.requests[0][3]);self.assertEqual([node.text for node in named(document,"AllowedMethod")],["GET","PUT"])
                self.assertEqual([node.text for node in named(document,"AllowedOrigin")],["*.get","*.put"])
                self.assertEqual(named(document,"MaxAgeSeconds")[0].text,"17")
                result=worker.call({"op":"api.GetBucketCors","input":{"Bucket":"fixture"}})
                self.assertTrue(result["ok"],result)
                self.assertEqual(result["data"]["CORSRules"],[{"ID":"server-rule","AllowedMethods":["HEAD"],"AllowedOrigins":["https://actual.example"],"MaxAgeSeconds":23}])

    def test_nested_lifecycle_dates_booleans_tags_and_large_integer(self):
        """Build actual nested SDK filters and consume independent date/boolean results; expect escaped tag bytes, exact 64-bit size and observed timestamp milliseconds."""
        configuration={"Rules":[{"ID":"rule","Status":"Enabled","Filter":{"And":{"Prefix":"raw/","Tags":[{"Key":"tag <&","Value":"value ü"}],"ObjectSizeGreaterThan":5000000001}},"Expiration":{"Date":"2030-01-01T00:00:00Z"}}]}
        response=b'<LifecycleConfiguration><Rule><ID>server-rule</ID><Status>Enabled</Status><Filter><Prefix>server/</Prefix></Filter><Expiration><Date>2030-01-01T00:00:00Z</Date></Expiration></Rule><Rule><ID>marker-rule</ID><Status>Disabled</Status><Filter/><Expiration><ExpiredObjectDeleteMarker>true</ExpiredObjectDeleteMarker></Expiration></Rule></LifecycleConfiguration>'
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{},b""),(200,{"Content-Type":"application/xml"},response)],"s3") as (worker,server):
                result=worker.call({"op":"api.PutBucketLifecycleConfiguration","input":{"Bucket":"fixture","LifecycleConfiguration":configuration}})
                self.assertTrue(result["ok"],result);document=tree(server.requests[0][3])
                self.assertEqual(named(document,"Key")[0].text,"tag <&");self.assertEqual(named(document,"Value")[0].text,"value ü")
                self.assertEqual(named(document,"ObjectSizeGreaterThan")[0].text,"5000000001")
                self.assertTrue(named(document,"Date")[0].text.startswith("2030-01-01T00:00:00"))
                result=worker.call({"op":"api.GetBucketLifecycleConfiguration","input":{"Bucket":"fixture"}})
                self.assertTrue(result["ok"],result);rules=result["data"]["Rules"]
                self.assertEqual(rules[0]["ID"],"server-rule");self.assertEqual(rules[0]["Filter"],{"Prefix":"server/"})
                self.assertEqual(rules[0]["Expiration"]["Date"],1893456000000)
                self.assertEqual(rules[1]["Expiration"]["ExpiredObjectDeleteMarker"],True)

    def test_policy_streaming_body_and_actual_json_response(self):
        """Use SDK-owned policy request/response payloads; expect original request JSON and an independently supplied response body, including preserved whitespace."""
        request='{"Version":"2012-10-17","Statement":[]}'
        response=' {"Version":"2012-10-17", "Statement":[], "Id":"server-only"}\n'
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(204,{},b""),(200,{"Content-Type":"application/json"},response.encode())],"s3") as (worker,server):
                result=worker.call({"op":"api.PutBucketPolicy","input":{"Bucket":"fixture","Policy":request}})
                self.assertTrue(result["ok"],result);self.assertEqual(server.requests[0][3],request.encode())
                result=worker.call({"op":"api.GetBucketPolicy","input":{"Bucket":"fixture"}})
                self.assertTrue(result["ok"],result);self.assertEqual(result["data"]["Policy"],response)

    def test_unknown_fields_scope_override_and_integer_narrowing_never_reach_service(self):
        """Supply unknown fields, conflicting scope and nonrepresentable SDK integers; expect explicit client errors with zero HTTP requests and no fabricated service status."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[],"s3") as (worker,server):
                requests=[{"op":"api.GetBucketCors","input":{"Bucket":"fixture","Unrecognized":"value"}},
                          {"op":"api.GetBucketCors","bucket":"owned","input":{"Bucket":"escape"}}]
                for number in (-2147483649,2147483648,2.5,True,"17"):
                    requests.append({"op":"api.PutBucketCors","input":{"Bucket":"fixture","CORSConfiguration":{"CORSRules":[{"AllowedMethods":["GET"],"AllowedOrigins":["*"],"MaxAgeSeconds":number}]}}})
                for number in (-9223372036854775809,9223372036854775808):
                    requests.append({"op":"api.PutBucketLifecycleConfiguration","input":{"Bucket":"fixture","LifecycleConfiguration":{"Rules":[{"Status":"Enabled","Filter":{"ObjectSizeGreaterThan":number}}]}}})
                for request in requests:
                    result=worker.call(request);self.assertFalse(result["ok"],(request,result));self.assertNotIn("status_code",result)
                self.assertEqual(server.requests,[])

    def test_integer_boundaries_and_null_optional_field_preserve_input(self):
        """Send signed-32-bit boundaries and omit a null optional integer; expect exact SDK wire values without narrowing or inventing zero."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{},b"")]*3,"s3") as (worker,server):
                for number in (-2147483648,2147483647,None):
                    result=worker.call({"op":"api.PutBucketCors","input":{"Bucket":"fixture","CORSConfiguration":{"CORSRules":[{"AllowedMethods":["GET"],"AllowedOrigins":["*"],"MaxAgeSeconds":number}]}}})
                    self.assertTrue(result["ok"],result)
                for number,request in zip((-2147483648,2147483647,None),server.requests):
                    nodes=named(tree(request[3]),"MaxAgeSeconds")
                    self.assertEqual([node.text for node in nodes],[] if number is None else [str(number)])

    def test_encryption_payload_and_false_flag_are_actual_sdk_fields(self):
        """Send KMS configuration with an explicit false flag and read an independent response; expect real model XML and unchanged server KMS/algorithm/boolean values."""
        configuration={"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms","KMSMasterKeyID":"request-only-key"},"BucketKeyEnabled":False}]}
        response=b'<ServerSideEncryptionConfiguration><Rule><ApplyServerSideEncryptionByDefault><SSEAlgorithm>aws:kms</SSEAlgorithm><KMSMasterKeyID>server-only-key</KMSMasterKeyID></ApplyServerSideEncryptionByDefault><BucketKeyEnabled>false</BucketKeyEnabled></Rule></ServerSideEncryptionConfiguration>'
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{},b""),(200,{"Content-Type":"application/xml"},response)],"s3") as (worker,server):
                result=worker.call({"op":"api.PutBucketEncryption","input":{"Bucket":"fixture","ServerSideEncryptionConfiguration":configuration}})
                self.assertTrue(result["ok"],result);document=tree(server.requests[0][3])
                self.assertEqual(named(document,"SSEAlgorithm")[0].text,"aws:kms");self.assertEqual(named(document,"KMSMasterKeyID")[0].text,"request-only-key")
                self.assertEqual(named(document,"BucketKeyEnabled")[0].text,"false")
                result=worker.call({"op":"api.GetBucketEncryption","input":{"Bucket":"fixture"}})
                self.assertTrue(result["ok"],result)
                self.assertEqual(result["data"]["ServerSideEncryptionConfiguration"],{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms","KMSMasterKeyID":"server-only-key"},"BucketKeyEnabled":False}]})

    def test_signed_64_bit_boundaries_preserve_exact_wire_numbers(self):
        """Pass both signed-64-bit endpoints into native SDK long fields; expect exact decimal XML rather than float conversion or overflow."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{},b"")]*2,"s3") as (worker,server):
                for number in (-9223372036854775808,9223372036854775807):
                    result=worker.call({"op":"api.PutBucketLifecycleConfiguration","input":{"Bucket":"fixture","LifecycleConfiguration":{"Rules":[{"Status":"Enabled","Filter":{"ObjectSizeGreaterThan":number}}]}}})
                    self.assertTrue(result["ok"],result)
                self.assertEqual([named(tree(request[3]),"ObjectSizeGreaterThan")[0].text for request in server.requests],["-9223372036854775808","9223372036854775807"])
