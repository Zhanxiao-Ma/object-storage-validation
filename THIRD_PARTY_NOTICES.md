# Upstream references and notices

The language-neutral scenario translations and test vectors in `osv/ceph_objects.py`, `osv/ceph_conditions.py`, `osv/ceph_metadata.py`, and `osv/ceph_copy.py` reference
Ceph s3-tests, revision `5b3987116eca0b4b8fa57d6b10f3db57bab196b6`.
Copyright (c) 2011 New Dream Network, LLC. The upstream permission notice is preserved in
[third_party/ceph-s3-tests-LICENSE](third_party/ceph-s3-tests-LICENSE).

The object checks and workload shape in `osv/objbench_cases.py` and `osv/objbench.py` reference
JuiceFS objbench, revision `29c60bbbeea410c6c0cb7a49540044e0c099dffb`.
JuiceFS: Copyright 2020 Juicedata, Inc., Apache License 2.0 (see [LICENSE](LICENSE)).
These are adapted scenario/phase implementations using our SDK protocol, not the upstream runners.

The pinned inventory in `osv/data/upstream.json` contains source identities, fingerprints and
parameter annotations for auditing. It does not execute upstream source code.
Vendor SDKs are obtained separately and retain their own licenses and notices.
