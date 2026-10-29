"""Retain missing required response fields as explicit contract failures, never defaults."""
class MissingResponseField(AssertionError):
    """Explain which SDK response field is absent while retaining a failing verdict and the available structure."""
    def __init__(self, operation, path, fields):
        self.failure_category = "response_field_missing"
        self.evidence = {"operation": operation, "missing_response_field": path,
                         "available_fields": sorted(fields)}
        super().__init__(f"{operation}: required response field {path} is absent; available fields={sorted(fields)!r}")

class ResponseFields(dict):
    """Track nested SDK field access without filling or normalizing values, so missing fields report their exact operation/path."""
    def __init__(self, value, operation, path="$"):
        super().__init__(value)
        self.operation, self.path = operation, path
    def __getitem__(self, key):
        if key not in self:
            raise MissingResponseField(self.operation, self.path + "." + str(key), self.keys())
        return observe(super().__getitem__(key), self.operation, self.path + "." + str(key))

def observe(value, operation, path="$"):
    """Preserve actual decoded values and list cardinality; annotate only required dictionary accesses."""
    if isinstance(value, dict):
        return ResponseFields(value, operation, path)
    if isinstance(value, list):
        return [observe(item, operation, f"{path}[{index}]") for index, item in enumerate(value)]
    return value
