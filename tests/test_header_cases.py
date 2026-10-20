"""Assert translated Ceph response-field invariants using real producer-consumer IO and faulted actual outputs."""
from pathlib import Path
import sys
import tempfile
import unittest
from osv.ceph_headers import CASES
from osv.scenarios import Context
from osv.suites import run_cases
from osv.worker import Worker
ROOT=Path(__file__).resolve().parents[1]

class HeaderCasesTest(unittest.TestCase):
    """Ensure each source invariant rejects a corrupted real response rather than merely executing SDK calls."""
    def test_each_field_invariant_rejects_wrong_actual_output(self):
        """Corrupt the ETag/cache/expiry/size/overwrite output after real IO; expect every corresponding production case to fail."""
        for index in range(5):
            with self.subTest(case=CASES[index].name),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary)
                worker=Worker({"command":[sys.executable,str(ROOT/"drivers/reference.py")],"env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},20)
                original=worker.call;reads=0
                def corrupt(request):
                    nonlocal reads
                    result=original(request)
                    if result["ok"]:
                        if index==0 and request["op"]=="put_http":result["etag"]="wrong-service-etag"
                        if index==1 and request["op"]=="head_http":result["http_headers"]["cache-control"]="private"
                        if index==2 and request["op"]=="head_http":result["http_headers"]["expires"]="Sat, 29 Oct 1994 19:43:31 GMT"
                        if index==3 and request["op"]=="head":result["size"]=1
                        if index==4 and request["op"]=="get":
                            reads+=1
                            if reads==2:Path(request["output_path"]).write_bytes(b"bar")
                    return result
                worker.call=corrupt
                try:
                    ctx=Context(worker,"headers/",directory,b"independent",directory/"body",directory/"output",{})
                    self.assertEqual(run_cases(ctx,(CASES[index],))[0]["status"],"fail")
                finally:worker.close()
