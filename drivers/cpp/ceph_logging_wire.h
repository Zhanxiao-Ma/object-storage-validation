#pragma once
// One synchronous compatibility RPC may override GET to POST before SDK signing.
// The scoped flag is restored on every return and exception, including nested calls.
inline thread_local bool osv_ceph_logging_post=false;
