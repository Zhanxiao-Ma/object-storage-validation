"""Run genuine original atomic SDK scenarios against explicitly concurrent Moto HTTP."""
from contextlib import closing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from local_moto import LocalMotoServer
from sdk_fixtures import ROOT
from osv.ceph_atomic_streams import CASES
from osv.scenarios import Context
from osv.suites import run_cases
from osv.worker import Worker

@unittest.skipUnless(os.environ.get("OSV_RUN_ATOMIC_STREAM_TESTS")=="1","enable actual atomic SDK integration")
class AtomicMotoTest(unittest.TestCase):
    """Keep streaming callbacks concurrent and independently verify all atomic cases; timings here never represent cloud performance."""
    def test_original_atomic_sources_on_explicit_threaded_moto(self):
        """Run all implemented original sizes/modes through actual Java/C++ SDKs and an explicitly threaded real Moto service; expect all sources including conditional PUT with the actual initialization ETag to pass, with cleanup and no serialized large-response deadlock."""
        server=LocalMotoServer(threaded=True);server.start()
        try:
            host,port=server.get_host_and_port()
            environment={"OSV_ACCESS_KEY":"local-test","OSV_SECRET_KEY":"local-test","OSV_SESSION_TOKEN":"","AWS_EC2_METADATA_DISABLED":"true","OSV_ENDPOINT":f"http://{host}:{port}","OSV_REGION":"us-east-1","OSV_BUCKET":"unused","OSV_PATH_STYLE":"true","OSV_RETRIES":"0","OSV_CONNECTIONS":"2","OSV_REQUEST_TIMEOUT_MS":"30000","OSV_CONNECT_TIMEOUT_MS":"2000"}
            with patch.dict(os.environ,environment):
                for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
                    command=["java","-Xmx256m","-jar",str(ROOT/"drivers/java/target/sdk-worker-0.1.0.jar"),"s3"] if language=="java" else [str(ROOT/"drivers/cpp/build/osv-cpp-s3")]
                    with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,closing(Worker({"command":command,"language":language,"api":"s3"},45)) as worker:
                        path=Path(temporary);context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",{})
                        observed=[];call=worker.call
                        def observe(request):
                            result=call(request)
                            if request.get("op")=="atomic_stream":observed.append((request,result))
                            return result
                        worker.call=observe
                        results=run_cases(context,CASES)
                        # The conditional request consumes the initial real PUT ETag.
                        expected=["pass" for row in CASES]
                        self.assertEqual([row["status"] for row in results],expected,results)
                        conditional=[result for request,result in observed if request["mode"]=="conditional_write"]
                        self.assertEqual(len(conditional),1);self.assertTrue(conditional[0]["ok"],conditional[0])
                        # Complete must contain only the latest successful PN1 ETag.
                        multipart=[result for request,result in observed if request["mode"]=="multipart_resend"]
                        self.assertEqual(len(multipart),1);self.assertTrue(multipart[0]["ok"],multipart)
                        self.assertEqual(multipart[0]["completed_entries"],1);self.assertEqual(multipart[0]["final_size"],8)
        finally:server.stop()
