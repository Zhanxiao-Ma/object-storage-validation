"""Validate rejection and readback independently so early assertions cannot hide mutations."""
import hashlib
from .scenarios import SDKOperationError


def check_rejected_mutation(bucket, key, response, status, code, expected=None, operation="mutation", get_operation="get_http"):
    """Always consume a real GET before judging the response; require exact rejection and unchanged/absent bytes."""
    path = bucket.probe.ctx.output
    path.unlink(missing_ok=True)
    observed = bucket.raw(get_operation, key=key, output_path=str(path))
    body = path.read_bytes() if observed.get("ok") and path.is_file() else None
    response_ok = response.get("ok") is False and response.get("status_code") == status and (code is None or response.get("error_code") == code)
    state_ok = (observed.get("ok") is False and observed.get("status_code") == 404 and observed.get("error_code") == "NoSuchKey") if expected is None else (observed.get("ok") is True and body == expected)
    evidence = {
        "operation": operation, "rejection_matches": response_ok, "object_state_matches": state_ok,
        "status_code": response.get("status_code"), "error_code": response.get("error_code"),
        "response_ok": response.get("ok"), "response_error": response.get("error"), "response_error_kind": response.get("error_kind"),
        "expected_status_code": status, "expected_error_code": code,
        "readback_ok": observed.get("ok"), "readback_error": observed.get("error"),
        "readback_status_code": observed.get("status_code"), "readback_error_code": observed.get("error_code"),
        "expected_object_state": "absent" if expected is None else "unchanged",
    }
    if body is not None:
        evidence.update(readback_size=len(body), readback_sha256=hashlib.sha256(body).hexdigest())
    if expected is not None:
        evidence.update(expected_size=len(expected), expected_sha256=hashlib.sha256(expected).hexdigest())
    if not response_ok or not state_ok:
        error = AssertionError(f"{operation}: rejection_matches={response_ok}, object_state_matches={state_ok}; "
                               f"response={response.get('status_code')}/{response.get('error_code')}, "
                               f"readback={observed.get('status_code')}/{observed.get('error_code')}")
        error.failure_category = (SDKOperationError(response).failure_category if not response.get("ok") and (response.get("status_code") or 0)<200
                                  else SDKOperationError(observed).failure_category if not observed.get("ok") and (observed.get("status_code") or 0)<200
                                  else "assertion_mismatch")
        error.evidence = evidence
        raise error
    return evidence
