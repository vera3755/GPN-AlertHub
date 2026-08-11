import os
import tempfile
import unittest
import server

class UnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.DB = os.path.join(self.tmp.name, "test_unit.db")
        server.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_tokenize_normalizes_synonyms(self):
        tokens = server.tokenize("Node SW-17 is unreachable")
        self.assertIn("host", tokens)
        self.assertIn("unavailable", tokens)
        self.assertIn("sw-17", tokens)

    def test_semantic_classification_host_unavailable(self):
        label, confidence = server.semantic_classify("Node SW-17 has stopped responding")
        self.assertEqual(label, "HOST_UNAVAILABLE")
        self.assertGreaterEqual(confidence, 0.90)

    def test_semantic_classification_disk(self):
        label, confidence = server.semantic_classify("Filesystem on DC-01 is almost full")
        self.assertEqual(label, "DISK_USAGE_HIGH")
        self.assertGreaterEqual(confidence, 0.90)

    def test_text_similarity_for_near_duplicates(self):
        score = server.text_similarity(
            "Host SW-17 is unavailable",
            "SW-17 host unavailable",
        )
        self.assertGreater(score, 0.70)

    def test_recipients_for_p2_dc01(self):
        event = {"host":"DC-01","service":"windows-infrastructure","severity":"P2"}
        names = {r["name"] for r in server.recipients(event)}
        self.assertIn("Олег Windows", names)
        self.assertNotIn("Олег Дежурный", names)

    def test_recipients_for_p0_drilling(self):
        event = {"host":"БУ-17","service":"drilling-control","severity":"P0"}
        names = {r["name"] for r in server.recipients(event)}
        self.assertIn("Вера Безопасник", names)
        self.assertIn("Олег Дежурный", names)

if __name__ == "__main__":
    unittest.main(verbosity=2)
