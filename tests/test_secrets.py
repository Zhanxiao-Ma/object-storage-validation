"""Exercise dynamic credential redaction through the real worker transport."""
import json
import sys
import unittest
from osv.cli import redactor
from osv.secrets import SecretRegistry, current_registry, secret_scope
from osv.worker import Worker


class DynamicSecretsTest(unittest.TestCase):
    """Verify new SDK credentials remain usable but cannot escape to diagnostics."""
    def test_nested_values_and_json_escapes(self):
        """Register nested raw credentials; expect literal and escaped text redacted without mutating payload."""
        registry=SecretRegistry()
        secret='new+secret/"\\雪'
        payload={"Credentials":{"AccessKeyId":"fresh-access-key", "SecretAccessKey":secret,"SessionToken":"fresh-token"}}
        registry.payload(payload)
        self.assertEqual(payload["Credentials"]["SecretAccessKey"],secret)
        for text in (secret,json.dumps(payload),json.dumps(payload,ensure_ascii=False)):
            scrubbed=registry.redact(text)
            self.assertNotIn(secret,scrubbed)
            self.assertNotIn("fresh-access-key",scrubbed)
            self.assertNotIn("fresh-token",scrubbed)
            self.assertIn("[REDACTED]",scrubbed)

    def test_scope_isolation_and_late_worker_credentials(self):
        """Consume a real child response then redact with an earlier closure; expect fresh credentials scrubbed and scopes restored."""
        # This fixture provides raw protocol input, not an implementation of any
        # AWS operation. Real SDK Query decoding has separate integration tests.
        program="""import json,sys
for line in sys.stdin:
 q=json.loads(line)
 if q['op']=='info':r={'ok':True}
 else:r={'ok':True,'elapsed_ns':1,'data':{'AccessKey':{'AccessKeyId':'new-test-access','SecretAccessKey':'new-test-secret','SessionToken':'new-test-token'}}}
 print(json.dumps(r),flush=True)
"""
        self.assertIsNone(current_registry())
        with secret_scope() as registry:
            scrub=redactor({})
            worker=Worker({"command":[sys.executable,"-c",program]},5)
            try:
                result=worker.call({"op":"api.iam.CreateAccessKey"})
                self.assertEqual(result["data"]["AccessKey"]["AccessKeyId"],"new-test-access")
                self.assertIs(worker.secret_registry,registry)
                self.assertNotIn("new-test-",scrub(json.dumps(result)))
                with secret_scope() as inner:
                    self.assertNotEqual(inner.redact("new-test-access"),"[REDACTED]")
                self.assertIs(current_registry(),registry)
            finally:worker.close()
        self.assertIsNone(current_registry())

    def test_diagnostic_and_fork_share_registry(self):
        """Issue a credential after worker startup; expect fork sharing and child stderr redacted on real exit."""
        program="""import json,sys
sys.stdin.readline();print(json.dumps({'ok':True}),flush=True)
sys.stdin.readline();print(json.dumps({'ok':True,'elapsed_ns':1,'Credentials':{'SecretAccessKey':'issued-later-secret'}}),flush=True)
sys.stdin.readline();print('issued-later-secret',file=sys.stderr,flush=True)
"""
        worker=Worker({"command":[sys.executable,"-c",program]},5)
        try:
            worker.call({"op":"issue"})
            child=worker.fork()
            try:self.assertIs(child.secret_registry,worker.secret_registry)
            finally:child.close()
            with self.assertRaises(RuntimeError) as failure:worker.call({"op":"exit"})
            self.assertIn("[REDACTED]",str(failure.exception))
            self.assertNotIn("issued-later-secret",str(failure.exception))
        finally:worker.close()
