import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main


class StartupTests(unittest.TestCase):
    def test_index_is_created_in_the_background_and_a_failure_does_not_stop_the_app(self):
        for failure in (None, RuntimeError("search is not ready yet")):
            with self.subTest(failure=failure):
                called = threading.Event()

                def ensure():
                    called.set()
                    if failure:
                        raise failure

                with patch.object(main, "ensure_indexes", side_effect=ensure), TestClient(main.app) as client:
                    self.assertTrue(called.wait(5))
                    self.assertEqual(client.get("/api/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
