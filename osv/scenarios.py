"""Language-neutral operation and compatibility-suite extension contracts."""
from dataclasses import dataclass
import hashlib
import importlib
import random
import re

BASE_OPERATIONS = frozenset({"put", "get", "head", "list", "delete", "mp_create", "mp_part", "mp_complete", "mp_abort"})


@dataclass(frozen=True)
class Context:
    """Provide scoped test data to plugins without coupling them to the scheduler."""
    worker: object
    prefix: str
    directory: object
    payload: bytes
    body: object
    output: object
    workload: dict
    round_index: int = 0
    index: int = 0


@dataclass(frozen=True)
class Operation:
    """Describe a measured request and its independent correctness oracle."""
    name: str
    required_operations: frozenset
    prepare: object
    verify: object
    throughput_metric: str = "verified_ops_s"


@dataclass(frozen=True)
class Suite:
    """Group semantic checks that can exercise multiple real SDK operations."""
    name: str
    required_operations: frozenset
    execute: object
    catalog: tuple = ()
    case_capabilities: bool = False


@dataclass(frozen=True)
class Profile:
    """Select reusable suites without binding test breadth to a language or SDK."""
    name: str
    suites: tuple
    description: str
    exclusions: tuple = ()


class Registry:
    """Keep extensions per run; reject ambiguous names instead of replacing built-ins."""
    def __init__(self):
        self.operations = {}
        self.suites = {}
        self.profiles = {}

    def _add(self, items, item):
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,63}", item.name) or item.name in items:
            raise ValueError(f"invalid or duplicate extension name: {item.name}")
        local_suite=isinstance(item,Suite) and item.case_capabilities and bool(item.catalog)
        if (not item.required_operations and not local_suite) or not all(isinstance(op, str) and op for op in item.required_operations):
            raise ValueError("extension must declare required worker operations")
        items[item.name] = item

    def add_operation(self, operation):
        if operation.throughput_metric not in ("verified_ops_s", "verified_mib_s"):
            raise ValueError("invalid throughput metric")
        if not callable(operation.prepare) or not callable(operation.verify):
            raise ValueError("operation requires callable prepare and verify")
        self._add(self.operations, operation)

    def add_profile(self, profile):
        if not profile.suites or len(set(profile.suites)) != len(profile.suites):
            raise ValueError("profile requires unique suite names")
        if any(name not in self.suites for name in profile.suites):
            raise ValueError("profile references unknown suite")
        # Profiles declare suites, not SDK operations.
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,63}", profile.name) or profile.name in self.profiles:
            raise ValueError("invalid or duplicate profile name")
        self.profiles[profile.name] = profile

    def add_suite(self, suite):
        if type(suite.case_capabilities) is not bool:
            raise ValueError("case_capabilities must be boolean")
        if suite.case_capabilities and not suite.catalog:
            raise ValueError("case capability checks require a declared catalog")
        if not callable(suite.execute):
            raise ValueError("suite requires callable execute")
        if suite.catalog:
            names = set()
            for case in suite.catalog:
                if (not isinstance(case, dict) or "gate" in case or not all(isinstance(case.get(key), str) and case[key]
                        for key in ("case", "purpose", "expected", "source", "reference"))
                        or case.get("severity", "error") not in ("error", "warning")):
                    raise ValueError("suite catalog requires case, purpose, expected, source, reference and valid source severity")
                if case["case"] in names:
                    raise ValueError("duplicate catalog case")
                names.add(case["case"])
        if suite.case_capabilities:
            operations=set()
            for case in suite.catalog:
                values=case.get("operations")
                if not isinstance(values,list) or not all(isinstance(op,str) and op for op in values):
                    raise ValueError("case capability checks require explicit operation lists")
                operations.update(values)
            if operations!=set(suite.required_operations):
                raise ValueError("suite operations must equal its case operation union")
        self._add(self.suites, suite)


def build_registry(config):
    registry = Registry()
    for name in ("get", "range", "put", "head", "list"):
        registry.add_operation(Operation(name, frozenset({"get" if name == "range" else name}),
            lambda ctx, name=name: prepare_builtin(ctx, name),
            lambda ctx, request, result, name=name: verify_builtin(ctx, name, request, result),
            "verified_mib_s" if name in ("get", "range", "put") else "verified_ops_s"))
    registry.add_suite(Suite("baseline", BASE_OPERATIONS,
        lambda ctx: validate_baseline(ctx.worker, ctx.prefix, ctx.directory, ctx.payload, ctx.workload["part_size"])))
    from .suites import register_suites
    register_suites(registry)
    plugins = config.get("plugins", [])
    if not isinstance(plugins, list) or not all(isinstance(p, str) and re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", p) for p in plugins):
        raise ValueError("plugins must be importable Python module names")
    if len(set(plugins)) != len(plugins):
        raise ValueError("duplicate plugin module")
    for module in plugins:
        importlib.import_module(module).register(registry)
    return registry


def prepare_builtin(ctx, name):
    request = {"op": "get" if name == "range" else name, "key": ctx.prefix+"perf",
               "body_path": str(ctx.body), "output_path": str(ctx.output), "prefix": ctx.prefix}
    if name == "range":
        rng = random.Random(ctx.workload["seed"] + ctx.round_index*ctx.workload["requests"] + ctx.index)
        request.update(offset=rng.randrange(len(ctx.payload)-ctx.workload["range_size"]+1), length=ctx.workload["range_size"])
    return request


def verify_builtin(ctx, name, request, result):
    if name in ("get", "range"):
        expected = ctx.payload if name == "get" else ctx.payload[request["offset"]:request["offset"]+request["length"]]
        assert_equal(hashlib.sha256(ctx.output.read_bytes()).digest(), hashlib.sha256(expected).digest())
        return len(expected)
    if name == "head":
        assert_equal(result["size"], len(ctx.payload))
    if name == "list":
        assert_equal(ctx.prefix+"perf" in result.get("keys", []), True)
    return len(ctx.payload) if name == "put" else 0


def require_ok(response):
    if not response["ok"]:
        raise RuntimeError(str(response.get("error", "SDK operation failed")))
    return response


def assert_equal(actual, expected):
    if actual != expected:
        raise AssertionError(f"unexpected result: {actual!r} != {expected!r}")


def validate_baseline(worker, prefix, directory, payload, part_size):
    """Real producer bytes are read back through the SDK; expected data is independent."""
    outcomes = []
    key = prefix + "compat"
    body = directory / "body.bin"
    output = directory / "read.bin"
    body.write_bytes(payload)
    def call(op, **kw):
        return require_ok(worker.call({"op": op, "key": key, **kw}))
    def case(name, action):
        try:
            action()
            outcomes.append({"case": name, "status": "pass"})
        except Exception as error:
            outcomes.append({"case": name, "status": "fail", "error": str(error)})
    def read_check(start=None, length=None):
        args = {"output_path": str(output)}
        if start is not None:
            args.update(offset=start, length=length)
        call("get", **args)
        assert_equal(output.read_bytes(), payload if start is None else payload[start:start+length])
    case("put", lambda: call("put", body_path=str(body)))
    case("head_size", lambda: assert_equal(call("head")["size"], len(payload)))
    case("get_content", read_check)
    case("range_first_byte", lambda: read_check(0, 1))
    case("range_last_byte", lambda: read_check(len(payload)-1, 1))
    case("range_middle", lambda: read_check(len(payload)//3, min(37, len(payload)-len(payload)//3)))
    # Preserve JuiceFS end-of-range clipping expectation and warning severity.
    try:
        read_check(len(payload)-1, 2)
        outcomes.append({"case": "range_end_overflow", "status": "pass", "behavior": "clipped", "severity": "warning"})
    except Exception as error:
        outcomes.append({"case": "range_end_overflow", "status": "warning", "behavior": str(error), "severity": "warning"})
    def listed():
        assert_equal(key in call("list", prefix=prefix).get("keys", []), True)
    case("list_contains_written_object", listed)
    empty = directory / "empty.bin"
    empty.write_bytes(b"")
    case("empty_put", lambda: call("put", key=prefix+"empty", body_path=str(empty)))
    case("delete", lambda: call("delete"))
    def missing():
        response = worker.call({"op": "head", "key": key})
        assert_equal(response.get("error_kind"), "not_found")
        assert_equal(response["ok"], False)
    case("head_deleted_object", missing)
    def multipart():
        upload_key = prefix + "multipart"
        upload = require_ok(worker.call({"op": "mp_create", "key": upload_key}))["upload_id"]
        try:
            first = directory / "part1.bin"
            second = directory / "part2.bin"
            first_data = random.Random(42).randbytes(part_size)
            second_data = b"tail-with-independent-content"
            first.write_bytes(first_data)
            second.write_bytes(second_data)
            parts = []
            for index, path in enumerate((first, second), 1):
                part = require_ok(worker.call({"op": "mp_part", "key": upload_key,
                            "upload_id": upload, "part_number": index, "body_path": str(path)}))
                parts.append({"part_number": index, "etag": part["etag"]})
            require_ok(worker.call({"op": "mp_complete", "key": upload_key,
                                  "upload_id": upload, "parts": parts}))
            require_ok(worker.call({"op": "get", "key": upload_key, "output_path": str(output)}))
            assert_equal(hashlib.sha256(output.read_bytes()).digest(), hashlib.sha256(first_data+second_data).digest())
        finally:
            # A completed ID may return not_found; cleanup of the committed object is separate.
            worker.call({"op": "mp_abort", "key": upload_key, "upload_id": upload})
    case("multipart_complete_content", multipart)
    def abort():
        upload_key = prefix + "abort"
        upload = require_ok(worker.call({"op": "mp_create", "key": upload_key}))["upload_id"]
        require_ok(worker.call({"op": "mp_abort", "key": upload_key, "upload_id": upload}))
        response = worker.call({"op": "head", "key": upload_key})
        assert_equal(response.get("error_kind"), "not_found")
    case("multipart_abort_no_committed_object", abort)
    return outcomes
