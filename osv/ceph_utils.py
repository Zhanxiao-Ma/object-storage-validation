"""Pinned Ceph utility generator and its exact size-boundary source assertions.

Source: ceph/s3-tests 5b3987116eca0b4b8fa57d6b10f3db57bab196b6,
s3tests/functional/utils.py and test_utils.py. This pure local utility case does
not exercise an object-store service and is reported separately from SDK IO.

Copyright (c) 2011 New Dream Network, LLC

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

"""
import random
import string
from .scenarios import Suite
from .suites import case,run_cases,CEPH

def generate_random(size, part_size=5*1024*1024):
    """
    Generate the specified number random data.
    (actually each MB is a repetition of the first KB)
    """
    chunk = 1024
    allowed = string.ascii_letters
    for x in range(0, size, part_size):
        strpart = ''.join([allowed[random.randint(0, len(allowed) - 1)] for _ in range(chunk)])
        s = ''
        left = size - x
        this_part_size = min(left, part_size)
        for y in range(this_part_size // chunk):
            s = s + strpart
        s = s + strpart[:(this_part_size % chunk)]
        yield s
        if (x == size):
            return


def generate(p):
    """Preserve all five original joined-length assertions at 0/1/5MiB±1; expect exact original byte counts without cloud requests."""
    FIVE_MB=5*1024*1024
    assert len(''.join(generate_random(0)))==0
    assert len(''.join(generate_random(1)))==1
    assert len(''.join(generate_random(FIVE_MB-1)))==FIVE_MB-1
    assert len(''.join(generate_random(FIVE_MB)))==FIVE_MB
    assert len(''.join(generate_random(FIVE_MB+1)))==FIVE_MB+1
    return {'execution_kind':'local_utility','cloud_requests':0,'original_sizes':[0,1,FIVE_MB-1,FIVE_MB,FIVE_MB+1]}


CASES=(case('ceph_utils_generate','Ceph本地随机数据生成器边界','保留原helper和全部五个长度断言；纯本地工具校验，不代表任何对象存储API通过','',generate,'test_generate',CEPH.replace('test_s3.py','test_utils.py')),)


def register(registry):
    registry.add_suite(Suite('ceph_utils',frozenset(),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
