"""Example extension: verify a full object listing through any language worker."""
from osv.scenarios import Operation, Suite, assert_equal, require_ok


def prepare_inventory(ctx):
    return {"op": "list", "prefix": ctx.prefix}


def verify_inventory(ctx, request, result):
    assert_equal(ctx.prefix+"perf" in result["keys"], True)
    return 0


def check_inventory(ctx):
    empty = ctx.directory/"inventory-empty.bin"
    empty.write_bytes(b"")
    key = ctx.prefix+"inventory-empty"
    require_ok(ctx.worker.call({"op": "put", "key": key, "body_path": str(empty)}))
    result = require_ok(ctx.worker.call(prepare_inventory(ctx)))
    assert_equal(key in result["keys"], True)
    return [{"case": "empty_object_is_listed", "status": "pass"}]


def register(registry):
    registry.add_operation(Operation("inventory", frozenset({"list"}),
                                     prepare_inventory, verify_inventory))
    registry.add_suite(Suite("inventory_semantics", frozenset({"put", "list"}), check_inventory))
