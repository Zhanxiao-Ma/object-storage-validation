"""Exercise real multipart producers and consumers with independently corrupted observations."""
from pathlib import Path
import sys
import tempfile
import unittest
from osv.ceph_multipart import CASES
from osv.scenarios import Context
from osv.suites import run_cases
from osv.worker import Worker
ROOT=Path(__file__).resolve().parents[1]

class MultipartExactTest(unittest.TestCase):
    """Verify exact source invariants and cleanup against actual filesystem upload/commit/read operations."""
    def test_corrupt_metadata_ranges_etag_and_errors_fail_and_clean(self):
        """Corrupt one real result after genuine IO; expect every independent Ceph assertion to fail and all owned files/uploads to disappear."""
        vectors=(("full","content_type"),("full","metadata"),("full","range_size"),("full","range_bytes"),("contents","etag"),("abort","contents"),("missing_part","status"),("etag","code"))
        for mode,broken in vectors:
            with self.subTest(mode=mode,broken=broken),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker({"command":[sys.executable,str(ROOT/"drivers/reference.py")],"env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},30)
                original=worker.call;faulted=[]
                def corrupt(request):
                    result=original(request)
                    if request["op"]=="get_metadata" and result["ok"]:
                        if broken=="content_type":result["content_type"]="wrong/type";faulted.append(True)
                        elif broken=="metadata":result["metadata"]={"wrong":"value"};faulted.append(True)
                        elif "offset" in request and broken=="range_size":result["size"]-=1;faulted.append(True)
                        elif "offset" in request and broken=="range_bytes":
                            output=Path(request["output_path"]);data=output.read_bytes();output.write_bytes(bytes([data[0]^1])+data[1:]);faulted.append(True)
                    if request["op"]=="mp_complete_observed":
                        if broken=="etag" and result["ok"]:result["etag"]="";faulted.append(True)
                        elif broken=="status" and not result["ok"]:result["status_code"]=500;faulted.append(True)
                        elif broken=="code" and not result["ok"]:result["error_code"]="WrongError";faulted.append(True)
                    if request["op"]=="list_v2_observed" and broken=="contents" and result["ok"]:result["fields"].append("Contents");faulted.append(True)
                    return result
                worker.call=corrupt
                try:
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    rows=run_cases(ctx,(next(c for c in CASES if c.name=="ceph_exact_multipart_"+mode),))
                    self.assertTrue(faulted);self.assertEqual(rows[0]["status"],"fail")
                    self.assertEqual([p for p in (directory/"objects").rglob("*") if p.is_file()],[])
                finally:worker.close()

    def test_actual_part_files_keep_original_six_size_vectors(self):
        """Record actual producer file lengths entering the real worker; expect all six unscaled Ceph part sequences and complete bucket cleanup."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);worker=Worker({"command":[sys.executable,str(ROOT/"drivers/reference.py")],"env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},30)
            original=worker.call;sizes=[];completed=[]
            def observe(request):
                if request["op"]=="mp_part":sizes.append((request["part_number"],Path(request["body_path"]).stat().st_size))
                if request["op"]=="mp_complete_observed":completed.append(tuple(p["part_number"] for p in request["parts"]))
                return original(request)
            worker.call=observe
            try:
                ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                rows=run_cases(ctx,(next(c for c in CASES if c.name=="ceph_exact_multipart_sizes"),))
                self.assertEqual(rows[0]["status"],"pass",rows)
                m=1024*1024
                self.assertEqual(sizes,[(1,5*m),(1,5*m),(2,100*1024),(1,5*m),(2,600*1024),(1,5*m),(2,5*m),(3,100*1024),(1,5*m),(2,5*m),(3,600*1024),(1,5*m),(2,5*m)])
                self.assertEqual(completed,[(1,),(1,2),(1,2),(1,2,3),(1,2,3),(1,2)])
                self.assertEqual([p for p in (directory/"objects").rglob("*") if p.is_file()],[])
            finally:worker.close()
