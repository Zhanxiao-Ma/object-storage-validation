"""Test standard S3 configuration without backend clocks, wire extensions or log-delivery deadlines."""
from contextlib import ExitStack
from copy import deepcopy
import json
from .bucket_scope import BucketScope, SCOPE_OPERATIONS
from .ceph_bucket_config import api, raw, rejected
from .scenarios import Suite, assert_equal
from .suites import case, run_cases

DOC = "https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketLifecycleConfiguration.html"
LOG_DOC = "https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketLogging.html"


def contains(actual, expected):
    """Compare requested standard fields while allowing service-supplied optional defaults."""
    if isinstance(expected, dict):
        assert_equal(isinstance(actual, dict), True)
        for key, value in expected.items():
            if key not in actual:
                raise AssertionError("missing configuration field: " + key)
            contains(actual[key], value)
    elif isinstance(expected, list):
        assert_equal(isinstance(actual, list), True)
        assert_equal(len(actual), len(expected))
        for result, wanted in zip(actual, expected):
            contains(result, wanted)
    else:
        assert_equal(actual, expected)


def lifecycle(p, fragment):
    with BucketScope(p) as bucket:
        rule = {"ID": "osv-rule", "Status": "Enabled", "Filter": {"Prefix": "data/"}, **deepcopy(fragment)}
        api(bucket, "PutBucketLifecycleConfiguration", LifecycleConfiguration={"Rules": [rule]})
        contains(api(bucket, "GetBucketLifecycleConfiguration")["data"]["Rules"], [rule])
        # Replacement is a real second PUT, not a merge into the previous configuration.
        replacement = {"ID": "osv-replaced", "Status": "Disabled", "Filter": {"Prefix": "other/"}, "Expiration": {"Days": 90}}
        api(bucket, "PutBucketLifecycleConfiguration", LifecycleConfiguration={"Rules": [replacement]})
        contains(api(bucket, "GetBucketLifecycleConfiguration")["data"]["Rules"], [replacement])
        api(bucket, "DeleteBucketLifecycle")
        rejected(raw(bucket, "GetBucketLifecycleConfiguration"), 404, "NoSuchLifecycleConfiguration")


def logging(p, key_format):
    with ExitStack() as stack:
        source, target = [stack.enter_context(BucketScope(p)) for _ in range(2)]
        policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
            "Principal": {"Service": "logging.s3.amazonaws.com"}, "Action": "s3:PutObject",
            "Resource": "arn:aws:s3:::" + target.name + "/logs/*",
            "Condition": {"ArnLike": {"aws:SourceArn": "arn:aws:s3:::" + source.name}}}]}
        try:
            api(target, "PutBucketPolicy", Policy=json.dumps(policy))
            enabled = {"TargetBucket": target.name, "TargetPrefix": "logs/"}
            if key_format:
                enabled["TargetObjectKeyFormat"] = deepcopy(key_format)
            api(source, "PutBucketLogging", BucketLoggingStatus={"LoggingEnabled": enabled})
            contains(api(source, "GetBucketLogging")["data"]["LoggingEnabled"], enabled)
            enabled["TargetPrefix"] = "logs/updated/"
            api(source, "PutBucketLogging", BucketLoggingStatus={"LoggingEnabled": enabled})
            contains(api(source, "GetBucketLogging")["data"]["LoggingEnabled"], enabled)
            api(source, "PutBucketLogging", BucketLoggingStatus={})
            assert_equal(bool(api(source, "GetBucketLogging")["data"].get("LoggingEnabled")), False)
        finally:
            # Stop asynchronous production before either owned bucket is removed.
            api(source, "PutBucketLogging", BucketLoggingStatus={})
            api(target, "DeleteBucketPolicy")


VECTORS = (
    ("expiration", {"Expiration": {"Days": 30}}),
    ("tag", {"Filter": {"Tag": {"Key": "archive", "Value": "yes"}}, "Expiration": {"Days": 30}}),
    ("and_tags", {"Filter": {"And": {"Prefix": "data/", "Tags": [{"Key": "a", "Value": "1"}, {"Key": "b", "Value": "2"}]}}, "Expiration": {"Days": 30}}),
    ("size", {"Filter": {"And": {"Prefix": "data/", "ObjectSizeGreaterThan": 1024, "ObjectSizeLessThan": 1048576}}, "Expiration": {"Days": 30}}),
    ("noncurrent_expiration", {"NoncurrentVersionExpiration": {"NoncurrentDays": 30, "NewerNoncurrentVersions": 2}}),
    ("delete_marker", {"Expiration": {"ExpiredObjectDeleteMarker": True}}),
    ("abort_multipart", {"AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}),
    ("transition", {"Transitions": [{"Days": 30, "StorageClass": "STANDARD_IA"}]}),
    ("noncurrent_transition", {"NoncurrentVersionTransitions": [{"NoncurrentDays": 30, "StorageClass": "STANDARD_IA"}]}),
)
LC_OPS = SCOPE_OPERATIONS + " api.PutBucketLifecycleConfiguration api.GetBucketLifecycleConfiguration api.DeleteBucketLifecycle"
LOG_OPS = SCOPE_OPERATIONS + " api.PutBucketPolicy api.DeleteBucketPolicy api.PutBucketLogging api.GetBucketLogging"
CASES = tuple(case("s3_lifecycle_configuration_" + name, "验证标准生命周期配置：" + name,
    "通过真实 PUT/GET 校验规则，再验证替换与删除；不使用加速时钟、不宣称后台已执行", LC_OPS,
    lambda p, value=value: lifecycle(p, value), "PutBucketLifecycleConfiguration", DOC) for name, value in VECTORS)
CASES += tuple(case("s3_logging_configuration_" + name, "验证标准访问日志配置：" + name,
    "通过真实 PUT/GET 校验目标、前缀和格式，再验证修改与停用；不要求日志定时交付或 Last-Modified",
    LOG_OPS + (" logging_target_key_format" if value else ""), lambda p, value=value: logging(p, value),
    "PutBucketLogging", LOG_DOC) for name, value in (
        ("default", None), ("simple", {"SimplePrefix": {}}),
        ("delivery_time", {"PartitionedPrefix": {"PartitionDateSource": "DeliveryTime"}}),
        ("event_time", {"PartitionedPrefix": {"PartitionDateSource": "EventTime"}})))


def register(registry):
    registry.add_suite(Suite("s3_configuration", frozenset(op for c in CASES for op in c.operations),
        lambda ctx: run_cases(ctx, CASES), tuple(c.metadata() for c in CASES), case_capabilities=True))
