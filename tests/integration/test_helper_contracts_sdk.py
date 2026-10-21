"""Exercise cleanup-helper consumers with real SDK producers and independent HTTP input."""
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from sdk_fixtures import fixture_worker
from osv.scenarios import Context
from osv.suites import Probe
from osv.s3_delete_compatibility import version_cursor


@unittest.skipUnless(os.environ.get("OSV_RUN_DELETE_TESTS") == "1", "enable after building actual AWS SDK workers")
class HelperContractsSdkTest(unittest.TestCase):
    def test_real_version_producers_feed_both_cursor_fields_and_empty_final_page(self):
        """Feed independent PUT/DELETE version IDs and XML pages through real SDKs into the production scenario; expect all IDs once, both actual cursor fields on following requests, acceptance of an optional empty final page, and rejection of missing/duplicate IDs, stalled cursors, empty truncated pages and oversized pages."""
        def page(body, truncated=False, key=None, version=None):
            tail = ("<NextKeyMarker>" + key + "</NextKeyMarker><NextVersionIdMarker>" + version + "</NextVersionIdMarker>") if key is not None else ""
            return (200, {"Content-Type": "application/xml"}, ("<ListVersionsResult><IsTruncated>" + str(truncated).lower() + "</IsTruncated>" + body + tail + "</ListVersionsResult>").encode())
        marker = "<DeleteMarker><Key>same-key</Key><VersionId>marker-id</VersionId><IsLatest>true</IsLatest></DeleteMarker>"
        old = "<Version><Key>same-key</Key><VersionId>old-id</VersionId><Size>3</Size></Version>"
        new = "<Version><Key>same-key</Key><VersionId>new-id</VersionId><Size>3</Size></Version>"
        last = "<Version><Key>z-next-key</Key><VersionId>other-id</VersionId><Size>3</Size></Version>"
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES", "java,cpp").split(","):
            for mode in ("normal", "empty_final", "missing", "duplicate", "stalled", "empty_truncated", "oversized"):
                empty_final = mode == "empty_final"
                invalid = mode not in ("normal", "empty_final")
                fixtures = [(404, {}, b""), (200, {}, b""), (200, {}, b"")]
                fixtures += [(200, {"x-amz-version-id": ident}, b"") for ident in ("old-id", "new-id", "other-id")]
                fixtures += [(204, {"x-amz-version-id": "marker-id", "x-amz-delete-marker": "true"}, b"")]
                pages = [page(marker, True, "same-key", "marker-id"), page(new, True, "same-key", "new-id"), page(old, True, "same-key", "old-id"), page(last)]
                if empty_final:
                    pages[-1:] = [page(last, True, "z-next-key", "other-id"), page("")]
                elif mode == "missing":
                    pages[-1] = page("")
                elif mode == "duplicate":
                    pages = pages[:1] + [page(marker)]
                elif mode == "stalled":
                    pages = pages[:1] + [page(new, True, "same-key", "marker-id")]
                elif mode == "empty_truncated":
                    pages = [page("", True, "same-key", "marker-id")]
                elif mode == "oversized":
                    pages = [page(marker + new)]
                fixtures += pages
                fixtures += [(200, {}, b""), page(marker + new + old + last)] + [(204, {}, b"")] * 4 + [page(""), (204, {}, b""), (404, {}, b"")]
                with self.subTest(language=language, mode=mode), tempfile.TemporaryDirectory() as temporary, fixture_worker(language, fixtures, "s3") as (worker, server):
                    directory = Path(temporary)
                    ctx = Context(worker, "unused/", directory, b"abc", directory/"body", directory/"output", {})
                    if invalid:
                        with self.assertRaises(AssertionError):
                            version_cursor(Probe(ctx))
                    else:
                        version_cursor(Probe(ctx))
                    self.assertFalse(server.fixtures)
                    if invalid:
                        continue
                    requests = [parse_qs(urlsplit(r[1]).query, keep_blank_values=True) for r in server.requests if r[0] == "GET" and parse_qs(urlsplit(r[1]).query).get("max-keys") == ["1"]]
                    self.assertEqual(len(requests), 5 if empty_final else 4)
                    expected = [("same-key", "marker-id"), ("same-key", "new-id"), ("same-key", "old-id")]
                    if empty_final:
                        expected += [("z-next-key", "other-id")]
                    self.assertEqual([(r["key-marker"][0], r["version-id-marker"][0]) for r in requests[1:]], expected)
