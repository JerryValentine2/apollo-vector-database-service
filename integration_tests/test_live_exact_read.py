"""Opt-in live exact-read verification. Never writes to Apollo.

Run only against a staging deployment of the PR code:
APOLLO_BASE_URL=https://... APOLLO_WRITE_KEY=... \\
APOLLO_TEST_PRODUCT_ID=... APOLLO_TEST_DOCUMENT_ID=... \\
python -m unittest discover -s integration_tests -v

The target document must already exist and be a non-sensitive Markdown/JSON
fixture. Do not print response bodies or credentials to CI logs.
"""
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest


class LiveExactRead(unittest.TestCase):
    def test_real_firestore_gcs_exact_read(self):
        names = (
            "APOLLO_BASE_URL", "APOLLO_WRITE_KEY",
            "APOLLO_TEST_PRODUCT_ID", "APOLLO_TEST_DOCUMENT_ID",
        )
        missing = [name for name in names if not os.getenv(name)]
        if missing:
            self.skipTest("Live integration environment not configured")
        base, key, product, document = (os.environ[name] for name in names)
        if not base.startswith("https://"):
            self.fail("Live integration requires HTTPS")
        path = "/products/{}/documents/{}/content".format(
            urllib.parse.quote(product, safe=""),
            urllib.parse.quote(document, safe=""),
        )
        url = base.rstrip("/") + path
        def call(headers):
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                return exc.code, json.load(exc)
        unauthorized_status, unauthorized = call({})
        self.assertEqual(unauthorized_status, 403)
        self.assertEqual(unauthorized.get("decision"), "STOP")
        status, payload = call({"X-Apollo-Write-Key": key})
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("decision"), "PASS")
        self.assertEqual(payload.get("product_id"), product)
        self.assertEqual(payload.get("document_id"), document)
        self.assertIn(payload.get("document_type"), ("markdown", "json"))
        content = payload.get("content")
        self.assertIsInstance(content, str)
        self.assertEqual(hashlib.sha256(content.encode("utf-8")).hexdigest(), payload.get("sha256"))


if __name__ == "__main__":
    unittest.main()
