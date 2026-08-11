import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
import server

class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.DB = os.path.join(cls.tmp.name, "test_integration.db")
        server.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        server.reset_demo()

    def test_process_event_end_to_end(self):
        result = server.process_event("Zabbix","БУ-17","Application unavailable","Disaster")
        self.assertFalse(result["duplicate"])
        self.assertGreaterEqual(result["deliveries"], 1)
        event = server.one("SELECT * FROM events WHERE id=?", (result["event_id"],))
        self.assertEqual(event["severity"], "P0")
        self.assertEqual(event["normalized_type"], "SERVICE_DEGRADED")
        self.assertEqual(event["pipeline_status"], "NORMALIZED → CORRELATED → ROUTED")

    def test_duplicate_is_suppressed(self):
        first = server.process_event("Zabbix","SW-17","Host unavailable","Disaster")
        second = server.process_event("Zabbix","SW-17","Host unavailable","Disaster")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["deliveries"], 0)

    def test_cascade_creates_one_incident(self):
        result = server.generate_cascade()
        self.assertEqual(len(result), 3)
        incident_ids = {x["incident_id"] for x in result}
        self.assertEqual(len(incident_ids), 1)
        iid = next(iter(incident_ids))
        incident = server.one("SELECT * FROM incidents WHERE id=?", (iid,))
        self.assertEqual(incident["severity"], "P0")
        self.assertEqual(incident["root_cause"], "SW-17")
        events = server.rows(
            "SELECT * FROM events WHERE incident_id=? AND is_duplicate=0", (iid,)
        )
        self.assertEqual(len(events), 3)

    def test_p2_routes_to_windows_user(self):
        result = server.process_event("Zabbix","DC-01","Disk usage 85%","Average")
        recipients = server.rows(
            """SELECT u.name FROM deliveries d
               JOIN users u ON u.id=d.user_id
               WHERE d.event_id=?""",
            (result["event_id"],),
        )
        self.assertEqual({r["name"] for r in recipients}, {"Олег Windows"})

    def test_http_api_end_to_end(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({
                "source":"Zabbix",
                "asset":"DC-01",
                "preset":"Disk usage 85%",
                "raw_severity":"Average"
            }).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/generate",
                data=body,
                headers={"Content-Type":"application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
            self.assertFalse(result["duplicate"])
            self.assertGreaterEqual(result["deliveries"], 1)

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state?user_id=3", timeout=5
            ) as response:
                state = json.loads(response.read().decode("utf-8"))
            self.assertEqual(state["user"]["name"], "Олег Windows")
            self.assertGreaterEqual(len(state["deliveries"]), 1)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

if __name__ == "__main__":
    unittest.main(verbosity=2)
