"""Corrupt actual local-list observations to verify production Ceph field and pagination assertions."""
from pathlib import Path
import sys
import tempfile
import unittest
from osv.ceph_list_exact import CASES
from osv.scenarios import Context
from osv.suites import run_cases
from osv.worker import Worker
ROOT=Path(__file__).resolve().parents[1]

class ExactListingTest(unittest.TestCase):
    """Require independent source expectations to reject wrong real results and still clean every owned bucket."""
    def test_field_presence_counts_echoes_and_owner_corruption_fail(self):
        """Corrupt one actual stored-list result per source invariant; expect a failed verdict and confirmed bucket removal in every case."""
        vectors=(("delimiter_none",2,"presence"),("delimiter_basic",2,"count"),("prefix_unreadable",1,"prefix"),("fetchowner_empty",2,"owner"),("delimiter_basic",1,"order"),("delimiter_prefix",1,"marker"),("continuationtoken_walk",2,"token"))
        for suffix,version,broken in vectors:
            with self.subTest(broken=broken),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary)
                worker=Worker({"command":[sys.executable,str(ROOT/"drivers/reference.py")],"env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},10)
                original=worker.call;owned=set()
                def corrupt(request):
                    result=original(request)
                    if request["op"] in ("list_v1_page","list_v2_observed") and result["ok"]:
                        owned.add(request["bucket"])
                        if broken=="presence":result["fields"].append("Delimiter");result["delimiter"]=""
                        elif broken=="count":result["key_count"]=999
                        elif broken=="prefix":result["prefix"]=""
                        elif broken=="owner":result["objects"][0]["owner"]={"id":"unexpected"}
                        elif broken=="order":result["common_prefixes"].reverse()
                        elif broken=="marker":result["next_marker"]="wrong-marker"
                        elif broken=="token" and "token" in request:result["token"]="wrong-token"
                    return result
                worker.call=corrupt
                try:
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    selected=next(case for case in CASES if case.name=="ceph_exact_list_v"+str(version)+"_"+suffix)
                    rows=run_cases(ctx,(selected,));self.assertEqual(rows[0]["status"],"fail",rows);self.assertTrue(owned)
                    for name in owned:self.assertEqual(worker.call({"op":"bucket_head","bucket":name})["status_code"],404)
                finally:worker.close()
