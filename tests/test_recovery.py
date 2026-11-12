import io
import json
import unittest
from unittest.mock import patch

import app as apollo
from apollo_v12 import file_type, extract_text_document, keyword_rank, fuse


class HelpersTests(unittest.TestCase):
    def test_supported_types(self):
        self.assertEqual(file_type("file.MD"), "markdown")
        self.assertEqual(file_type("file.JSON"), "json")
        with self.assertRaises(ValueError):
            file_type("file.exe")

    def test_json_validation(self):
        pages = extract_text_document(b'{"hello": "world"}', "json")
        self.assertIn("world", pages[0]["text"])
        with self.assertRaises(ValueError):
            extract_text_document(b"{bad", "json")

    def test_invalid_utf8(self):
        with self.assertRaises(ValueError):
            extract_text_document(bytes([255]), "markdown")

    def test_keyword_ranking(self):
        chunks = [
            {"chunk_id": "a", "content": "alpha beta"},
            {"chunk_id": "b", "content": "gamma"},
        ]
        results = keyword_rank(chunks, "alpha", 5)
        self.assertEqual([x["chunk_id"] for x in results], ["a"])

    def test_fusion_distance_filter(self):
        result = fuse([], [{"document_id": "d", "chunk_id": "c", "vector_distance": 0.9}], 5, 0.6)
        self.assertEqual(result, [])


class HTTPContractTests(unittest.TestCase):
    def setUp(self):
        self.client = apollo.app.test_client()

    def test_health_contract(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["version"], "1.2")
        self.assertIn("hybrid_search", data["capabilities"])

    def test_write_denied_without_key(self):
        with patch.object(apollo, "WRITE_KEY", "test-only-key"):
            response = self.client.post("/documents", json={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["decision"], "STOP")

    def test_invalid_write_key_denied(self):
        with patch.object(apollo, "WRITE_KEY", "test-only-key"):
            response = self.client.post("/documents", json={}, headers={"X-Apollo-Write-Key": "wrong"})
        self.assertEqual(response.status_code, 403)

    def test_missing_write_key_configuration(self):
        with patch.object(apollo, "WRITE_KEY", ""):
            response = self.client.post("/documents", json={})
        self.assertEqual(response.status_code, 503)

    def test_identifier_validation(self):
        self.assertIsNotNone(apollo.validate_identifier("a/b", "product_id"))
        self.assertIsNone(apollo.validate_identifier("morpheus_adaptive_ui", "product_id"))

    def test_read_route_registered(self):
        rules = {rule.rule for rule in apollo.app.url_map.iter_rules()}
        self.assertIn("/products/<product_id>/documents/<document_id>", rules)
        self.assertNotIn("/products/<product_id>/documents/<document_id>/content", rules)


if __name__ == "__main__":
    unittest.main()
