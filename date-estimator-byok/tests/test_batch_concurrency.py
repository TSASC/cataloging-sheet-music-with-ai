"""Reproduces and guards against the batch-run lost-update race (issue #9).

The batch transcription run stores results in batch_results/<run_id>.json.
Each "Add to Run" did load -> append -> save. With 2 gunicorn workers and no
locking, concurrent/overlapping adds clobber each other: the canonical outcome
is the penultimate scan being lost while the last one survives, plus the
client's run_count check then firing the "fatal error" dialog.
"""
import os
import shutil
import tempfile
import threading
import unittest

import app as appmod


class BatchConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self._orig_dir = appmod.BATCH_RESULTS_DIR
        self._orig_testing = appmod.app.config.get("TESTING")
        self.tmp = tempfile.mkdtemp(prefix="batch_results_test_")
        appmod.BATCH_RESULTS_DIR = self.tmp
        appmod.app.config["TESTING"] = True

    def tearDown(self):
        appmod.BATCH_RESULTS_DIR = self._orig_dir
        appmod.app.config["TESTING"] = self._orig_testing
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hammer(self, run_id, n_results, appender):
        appmod._save_batch_results(run_id, [])
        barrier = threading.Barrier(n_results)

        def worker(i):
            barrier.wait()  # maximize overlap
            appender(run_id, {"image_name": "scan_%03d.jpg" % i, "i": i})

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_results)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return appmod._load_batch_results(run_id)

    @staticmethod
    def _unlocked_append(run_id, result):
        """The exact pattern the route used before the fix."""
        results = appmod._load_batch_results(run_id)
        results.append(result)
        appmod._save_batch_results(run_id, results)

    def test_unlocked_append_loses_updates(self):
        """Demonstrates the bug: naive load/append/save loses results."""
        # Run several rounds; the race is nondeterministic but reliably fires
        # at least once across rounds.
        lost_at_least_once = False
        for _ in range(5):
            final = self._hammer("repro_unlocked", 25, self._unlocked_append)
            if len(final) < 25:
                lost_at_least_once = True
                break
        self.assertTrue(
            lost_at_least_once,
            "Expected lost updates from the unlocked append across 5 rounds",
        )

    def test_locked_append_preserves_all(self):
        """The fix: atomic, locked append keeps every result under concurrency."""
        final = self._hammer("repro_locked", 25, appmod._append_batch_result)
        self.assertEqual(len(final), 25)
        names = sorted(r["image_name"] for r in final)
        self.assertEqual(names, sorted("scan_%03d.jpg" % i for i in range(25)))

    def test_locked_append_is_idempotent(self):
        """Re-adding the same result (retry/double-click) does not duplicate."""
        appmod._save_batch_results("idem", [])
        r = {"image_name": "scan_001.jpg", "dedupe_key": "abc-123"}
        appmod._append_batch_result("idem", r)
        appmod._append_batch_result("idem", r)
        final = appmod._load_batch_results("idem")
        self.assertEqual(len(final), 1)

    def test_end_run_preserves_results_for_later_export(self):
        run_id = "ended_run"
        appmod._save_batch_results(run_id, [])
        appmod._append_batch_result(
            run_id,
            {
                "title": "Sonata",
                "statement_of_responsibility": "A. Composer",
                "edition": "",
                "city": "Boston",
                "publisher_name": "Publisher",
                "address": "1 Main St",
                "plate_number": "123",
                "date": "1901",
                "image_name": "scan_001.jpg",
            },
        )

        with appmod.app.test_client() as client:
            with client.session_transaction() as sess:
                sess["batch_folder_path"] = self.tmp
                sess["current_run"] = {
                    "id": run_id,
                    "started_at": "2026-06-22T12:00:00",
                }

            response = client.post("/batch-transcription", data={"action": "end_run"})
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Saved Runs", response.data)
            self.assertIn(run_id.encode(), response.data)

            with client.session_transaction() as sess:
                self.assertNotIn("current_run", sess)

            final = appmod._load_batch_results(run_id)
            self.assertEqual(len(final), 1)

            export = client.get(f"/export-run/csv?run_id={run_id}")
            self.assertEqual(export.status_code, 200)
            self.assertIn(b"Sonata", export.data)
            self.assertIn(
                f'batch_transcription_{run_id}.csv'.encode(),
                export.headers["Content-Disposition"].encode(),
            )


if __name__ == "__main__":
    unittest.main()
