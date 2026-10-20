"""Run translated Ceph HTTP-range cases through the actual filesystem reference producer and consumer."""
from pathlib import Path
import sys
import tempfile
import unittest
from osv.ceph_range import CASES
from osv.scenarios import Context
from osv.suites import run_cases
from osv.worker import Worker

ROOT=Path(__file__).resolve().parents[1]

class RangeHttpTest(unittest.TestCase):
    """Check strict Range case execution and independent response-field failure detection without duplicating range parsing."""
    def test_all_source_ranges_use_real_stored_data(self):
        """Execute all six source vectors using genuine filesystem IO; expect every content/header/status assertion to pass."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            worker=Worker({"language":"python","api":"local","command":[sys.executable,str(ROOT/"drivers/reference.py")],"env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},20)
            try:
                ctx=Context(worker,"ranges/",directory,b"irrelevant",directory/"body",directory/"output",{})
                rows=run_cases(ctx,CASES)
                self.assertEqual([row["status"] for row in rows],["pass"]*6,rows)
            finally:worker.close()

    def test_wrong_status_header_and_content_are_rejected(self):
        """Corrupt one genuine consumer result at a time; expect each independent Ceph status/header/body invariant to fail."""
        for broken in ("status","header","body"):
            with self.subTest(broken=broken),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary)
                worker=Worker({"language":"python","api":"local","command":[sys.executable,str(ROOT/"drivers/reference.py")],"env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},20)
                original=worker.call
                def corrupt(request):
                    result=original(request)
                    if request["op"]=="get_http" and result["ok"]:
                        if broken=="status":result["status_code"]=200
                        elif broken=="header":result["http_headers"]["content-range"]="bytes 0-3/11"
                        else:Path(request["output_path"]).write_bytes(b"wrong")
                    return result
                worker.call=corrupt
                try:
                    ctx=Context(worker,"ranges/",directory,b"irrelevant",directory/"body",directory/"output",{})
                    self.assertEqual(run_cases(ctx,CASES[:1])[0]["status"],"fail")
                finally:worker.close()
