"""Independent policy wire text enters the production common worker consumer."""
import json
import unittest
from urllib.parse import quote
from osv.worker import normalize_control_documents, normalize_control_result


class ControlDocumentsTest(unittest.TestCase):
    """Prevent typed policy text from losing order or literal plus/percent semantics."""
    def test_plain_encoded_and_nested_documents_preserve_wire_order(self):
        """Consume genuine-format text with deliberately nonalphabetic order; expect original insertion order and exactly one RFC3986 decode."""
        raw='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"a+b%2Bc"}]}'
        for text in (raw,quote(raw,safe="")):
            with self.subTest(encoded=text!=raw):
                response={"Role":{"AssumeRolePolicyDocument":text},"Policies":[{"PolicyDocument":text}],"Other":"untouched"}
                normalize_control_documents(response)
                self.assertEqual(json.dumps(response["Role"]["AssumeRolePolicyDocument"],separators=(",",":")),raw)
                self.assertEqual(list(response["Policies"][0]["PolicyDocument"]),["Version","Statement"])
                self.assertEqual(response["Role"]["AssumeRolePolicyDocument"]["Statement"][0]["Action"],"a+b%2Bc")
                self.assertEqual(response["Other"],"untouched")
                normalize_control_documents(response)
                self.assertEqual(json.dumps(response["Role"]["AssumeRolePolicyDocument"],separators=(",",":")),raw)

    def test_invalid_policy_shapes_fail_without_silent_empty_documents(self):
        """Pass actual malformed/nonobject raw values to the consumer; expect errors rather than fabricated policy objects."""
        for text in ("[]","null","true","123",'"policy"',"%ZZ","%FF","{"):
            with self.subTest(text=text),self.assertRaises((ValueError,UnicodeError)):
                normalize_control_documents({"PolicyDocument":text})

    def test_mixed_concurrent_results_follow_actual_requested_service(self):
        """Pass mixed actual RPC requests and raw responses; expect only corresponding IAM policies decoded, S3 bytes untouched and successful count mismatch rejected."""
        raw='{"Version":"v","Statement":[]}'
        request={"op":"concurrent_requests","requests":[{"op":"api.GetBucketPolicy"},{"op":"api.iam.GetUserPolicy"},{"op":"api.sts.GetCallerIdentity"}]}
        response={"ok":True,"results":[{"data":{"PolicyDocument":raw}},{"data":{"PolicyDocument":raw}},{"ok":False,"error_code":"AccessDenied"}]}
        normalize_control_result(request,response)
        self.assertEqual(response["results"][0]["data"]["PolicyDocument"],raw)
        self.assertEqual(list(response["results"][1]["data"]["PolicyDocument"]),["Version","Statement"])
        self.assertEqual(response["results"][2],{"ok":False,"error_code":"AccessDenied"})
        for rows in ([],{},[{}]):
            with self.subTest(rows=rows),self.assertRaises(ValueError):normalize_control_result(request,{"ok":True,"results":rows})
        normalize_control_result(request,{"ok":False,"error_code":"InvalidRequest"})
