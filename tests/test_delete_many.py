"""Reject corrupted bulk acknowledgments and field presence after genuine local deletion."""
from pathlib import Path
import sys
import tempfile
import unittest
from osv.ceph_delete_many import CASES
from osv.scenarios import Context
from osv.suites import run_cases
from osv.worker import Worker
ROOT=Path(__file__).resolve().parents[1]

class DeleteManyTest(unittest.TestCase):
    """Exercise the actual bulk producer/consumer and independent source expectations, with owned-resource cleanup on failure."""
    def test_wrong_ack_error_flag_and_contents_presence_fail(self):
        """Fault one actual bulk/list result after real file deletion; expect each source invariant to fail and the test bucket to be removed."""
        for broken in ("ack","errors","contents"):
            with self.subTest(broken=broken),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker({"command":[sys.executable,str(ROOT/"drivers/reference.py")],"env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},10)
                original=worker.call;owned=set()
                def corrupt(request):
                    result=original(request)
                    if result["ok"] and request["op"]=="delete_many":
                        owned.add(request["bucket"])
                        if broken=="ack":result["deleted"]=result["deleted"][:2]
                        elif broken=="errors":result["errors_present"]=True
                    if result["ok"] and request["op"]=="list_v1_page" and broken=="contents" and not result["objects"]:result["fields"].append("Contents")
                    return result
                worker.call=corrupt
                try:
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    rows=run_cases(ctx,(CASES[0],));self.assertEqual(rows[0]["status"],"fail",rows)
                    for name in owned:self.assertEqual(worker.call({"op":"bucket_head","bucket":name})["status_code"],404)
                finally:worker.close()
