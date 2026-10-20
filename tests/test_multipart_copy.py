"""Reject corrupted real multipart-copy, upload-list and PartNumber observations."""
from pathlib import Path
import sys
import tempfile
import unittest
from osv.ceph_multipart_copy import CASES as COPY
from osv.ceph_get_part import CASES as GET
from osv.ceph_multipart import source_data
from osv.scenarios import Context
from osv.suites import run_cases
from osv.worker import Worker
ROOT=Path(__file__).resolve().parents[1]

class MultipartCopyTest(unittest.TestCase):
    """Verify real producer bytes pass through copy/complete/read and that original source assertions reject corrupted results."""
    def test_wrong_copy_list_and_part_results_fail_without_leaks(self):
        """Alter one result after true filesystem IO; expect independent byte/size/error/ID/count/ETag oracles to fail and every bucket/upload to be removed."""
        vectors=(("ceph_exact_multipart_copy_small","bytes"),("ceph_exact_multipart_copy_small","size"),
                 ("ceph_exact_multipart_copy_invalid","status"),("ceph_exact_multipart_copy_improper","code"),
                 ("ceph_exact_multipart_list","ids"),("ceph_get_part_single","parts_count"),
                 ("ceph_get_part_single","etag"),("ceph_get_part_single","part_bytes"),
                 ("ceph_get_part_ordinary","ordinary_etag"))
        for name,broken in vectors:
            with self.subTest(case=name,broken=broken),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker({"command":[sys.executable,str(ROOT/"drivers/reference.py")],"env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},30)
                original=worker.call;faulted=[]
                def corrupt(request):
                    result=original(request)
                    if request["op"]=="get_metadata" and result["ok"]:
                        if broken=="bytes":
                            output=Path(request["output_path"]);data=output.read_bytes();output.write_bytes(bytes([data[0]^1])+data[1:]);faulted.append(True)
                        elif broken=="size":result["size"]+=1;faulted.append(True)
                    if request["op"]=="mp_copy" and not result["ok"]:
                        if broken=="status":result["status_code"]=500;faulted.append(True)
                        elif broken=="code":result["error_code"]="WrongError";faulted.append(True)
                    if request["op"]=="mp_list" and result["ok"] and broken=="ids":result["uploads"]=result["uploads"][:-1];faulted.append(True)
                    if request["op"]=="head_part" and result["ok"]:
                        if broken=="parts_count":result["parts_count"]+=1;faulted.append(True)
                        elif broken=="etag":result["etag"]="other-object-etag";faulted.append(True)
                    if request["op"]=="get_part" and result["ok"]:
                        if broken=="part_bytes":
                            output=Path(request["output_path"]);data=output.read_bytes();output.write_bytes(bytes([data[0]^1])+data[1:]);faulted.append(True)
                        elif broken=="ordinary_etag":result["etag"]="wrong-object-etag";faulted.append(True)
                    return result
                worker.call=corrupt
                try:
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    rows=run_cases(ctx,(next(c for c in COPY+GET if c.name==name),))
                    self.assertTrue(faulted);self.assertEqual(rows[0]["status"],"fail")
                    self.assertEqual([file for file in (directory/"objects").rglob("*") if file.is_file()],[])
                finally:worker.close()

    def test_original_ascii_payload_shape_is_not_scaled(self):
        """Generate a boundary-spanning original-shaped input; expect ASCII letters, 1 KiB repetition within each part and a distinct next-part pattern."""
        data=source_data(5*1024*1024+123)
        self.assertEqual(len(data),5*1024*1024+123)
        self.assertTrue(data.isalpha())
        self.assertEqual(data[:1024],data[1024:2048])
        self.assertEqual(data[:1024],data[5*1024*1024-1024:5*1024*1024])
        self.assertNotEqual(data[:123],data[-123:])
