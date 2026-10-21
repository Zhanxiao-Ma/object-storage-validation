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

`drivers/schema/bucket-api.json` contains a documentation-stripped subset of the
AWS SDK for C++ 1.11.675 S3 API model. The source URL, full-source SHA-256 and
explicit C++ payload bindings are retained in that file. This is interface metadata
used to generate typed SDK builders/getters and operation allowlists; it contains
no AWS official test cases. The source AWS SDK is distributed under Apache-2.0;
its license is preserved in
[third_party/aws-sdk-api-model/LICENSE](third_party/aws-sdk-api-model/LICENSE).

All `osv/ceph_*.py` translations, including later bucket, multipart and versioning
groups, retain the same pinned Ceph provenance and permission notice above.

MinIO Java SDK and MinIO C++ SDK are obtained separately from [minio/minio-java](https://github.com/minio/minio-java) and [minio/minio-cpp](https://github.com/minio/minio-cpp), both under Apache-2.0. SDK artifacts retain their own license and notice files; no SDK implementation is vendored here.
