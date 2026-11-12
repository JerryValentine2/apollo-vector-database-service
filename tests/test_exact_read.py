import hashlib
import unittest
from unittest.mock import MagicMock, patch
import app as apollo


class ExactReadTests(unittest.TestCase):
    def setUp(self):
        self.client = apollo.app.test_client()
        self.url = "/products/demo/documents/doc1/content"
        self.key = {"X-Apollo-Write-Key": "test-secret"}
        self.raw = b"# Hello"
        self.record = {
            "product_id": "demo", "document_id": "doc1",
            "document_type": "markdown",
            "source_uri": "gs://test-bucket/demo/doc1/source.md",
            "source_sha256": hashlib.sha256(self.raw).hexdigest(),
        }

    def fake_clients(self, record=None, exists=True, raw=None):
        snapshot = MagicMock()
        snapshot.exists = exists
        snapshot.to_dict.return_value = record if record is not None else self.record
        db = MagicMock()
        storage = MagicMock()
        storage.bucket.return_value.blob.return_value.download_as_bytes.return_value = self.raw if raw is None else raw
        return db, storage, snapshot

    def test_no_key_denied(self):
        with patch.object(apollo, "WRITE_KEY", "test-secret"):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_exact_content_success(self):
        db, storage, snapshot = self.fake_clients()
        with patch.object(apollo, "WRITE_KEY", "test-secret"), patch.object(apollo, "DOCUMENT_BUCKET", "test-bucket"), patch.object(apollo, "get_clients", return_value=(None, db, storage)), patch.object(apollo, "get_document", return_value=(None, snapshot)):
            response = self.client.get(self.url, headers=self.key)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["content"], "# Hello")

    def test_missing_document(self):
        db, storage, snapshot = self.fake_clients(exists=False)
        with patch.object(apollo, "WRITE_KEY", "test-secret"), patch.object(apollo, "DOCUMENT_BUCKET", "test-bucket"), patch.object(apollo, "get_clients", return_value=(None, db, storage)), patch.object(apollo, "get_document", return_value=(None, snapshot)):
            response = self.client.get(self.url, headers=self.key)
        self.assertEqual(response.status_code, 404)

    def test_tampered_source_rejected(self):
        db, storage, snapshot = self.fake_clients(raw=b"tampered")
        with patch.object(apollo, "WRITE_KEY", "test-secret"), patch.object(apollo, "DOCUMENT_BUCKET", "test-bucket"), patch.object(apollo, "get_clients", return_value=(None, db, storage)), patch.object(apollo, "get_document", return_value=(None, snapshot)):
            response = self.client.get(self.url, headers=self.key)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["decision"], "STOP")

    def test_scope_mismatch_rejected(self):
        bad = dict(self.record, product_id="other")
        db, storage, snapshot = self.fake_clients(record=bad)
        with patch.object(apollo, "WRITE_KEY", "test-secret"), patch.object(apollo, "DOCUMENT_BUCKET", "test-bucket"), patch.object(apollo, "get_clients", return_value=(None, db, storage)), patch.object(apollo, "get_document", return_value=(None, snapshot)):
            response = self.client.get(self.url, headers=self.key)
        self.assertEqual(response.status_code, 409)


    def test_wrong_key_rejected_without_storage_access(self):
        with patch.object(apollo, "WRITE_KEY", "test-secret"), patch.object(apollo, "get_clients") as clients:
            response = self.client.get(self.url, headers={"X-Apollo-Write-Key": "incorrect"})
        self.assertEqual(response.status_code, 403)
        clients.assert_not_called()

    def test_missing_server_key_fails_closed(self):
        with patch.object(apollo, "WRITE_KEY", ""), patch.object(apollo, "get_clients") as clients:
            response = self.client.get(self.url, headers=self.key)
        self.assertEqual(response.status_code, 503)
        clients.assert_not_called()

    def test_cross_document_source_uri_rejected(self):
        bad = dict(self.record, source_uri="gs://test-bucket/other/doc1/source.md")
        db, storage, snapshot = self.fake_clients(record=bad)
        with patch.object(apollo, "WRITE_KEY", "test-secret"), patch.object(apollo, "DOCUMENT_BUCKET", "test-bucket"), patch.object(apollo, "get_clients", return_value=(None, db, storage)), patch.object(apollo, "get_document", return_value=(None, snapshot)):
            response = self.client.get(self.url, headers=self.key)
        self.assertEqual(response.status_code, 409)
        storage.bucket.assert_not_called()

    def test_invalid_document_identity_rejected_without_storage_access(self):
        with patch.object(apollo, "WRITE_KEY", "test-secret"), patch.object(apollo, "get_clients") as clients:
            response = self.client.get("/products/demo/documents/%20/content", headers=self.key)
        self.assertEqual(response.status_code, 400)
        clients.assert_not_called()

if __name__ == "__main__":
    unittest.main()
