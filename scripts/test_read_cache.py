import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mayor_tools  # noqa: E402

NETWORKS = ("GET", "/state/networks")


class ReadCacheTests(unittest.TestCase):
    """A wake's large reads are reused between changes, and never across one."""

    def setUp(self):
        self.now = 1000.0
        self.sent = []
        patches = [mock.patch.object(mayor_tools.mayor_transport, "request", side_effect=self._bridge),
                   mock.patch.object(mayor_tools.time, "monotonic", side_effect=lambda: self.now)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(mayor_tools.enable_read_cache, False)

    def _bridge(self, base_url, method, path, *, query=None, body=None):
        self.sent.append((method, path))
        if path == "/commands/fail":
            raise mayor_tools.BridgeRequestError({"ok": False, "error": "refused"})
        return {"ok": True, "segments": [{"id": len(self.sent)}]}

    def _paused_and_settled(self):
        mayor_tools.enable_read_cache()
        mayor_tools.call_tool("set_simulation_speed", {"paused": True})
        self.now += mayor_tools.READ_CACHE_SETTLE_SECONDS

    def _read(self, query=None):
        return mayor_tools._request(*NETWORKS, query=query or {"limit": 20000})

    def test_off_unless_a_process_turns_it_on(self):
        mayor_tools.call_tool("set_simulation_speed", {"paused": True})
        self.now += 60
        self._read()
        self._read()
        self.assertEqual(2, self.sent.count(NETWORKS))

    def test_a_repeated_read_is_answered_once_and_each_caller_gets_its_own_copy(self):
        self._paused_and_settled()
        first = self._read()
        first["segments"].clear()
        second = self._read()
        self.assertEqual(1, self.sent.count(NETWORKS))
        self.assertEqual([{"id": 2}], second["segments"])
        self._read({"limit": 20000, "service": "Road"})  # another query is another read
        self.assertEqual(2, self.sent.count(NETWORKS))

    def test_every_change_empties_it_including_a_refused_one(self):
        for change in (lambda: mayor_tools._request("POST", "/commands/bulldoze", body={"id": 1}),
                       lambda: mayor_tools._request("POST", "/commands/set-zone", body={"dryRun": True}),
                       lambda: mayor_tools._request("POST", "/commands/fail", body={})):
            with self.subTest():
                self.sent.clear()
                self._paused_and_settled()
                self._read()
                try:
                    change()
                except mayor_tools.BridgeRequestError:
                    pass
                self.now += mayor_tools.READ_CACHE_SETTLE_SECONDS
                self._read()
                self.assertEqual(2, self.sent.count(NETWORKS))

    def test_a_mutating_tool_empties_it(self):
        self._paused_and_settled()
        self._read()
        with mock.patch.object(mayor_tools, "_require_normal_progression"), \
                mock.patch.object(mayor_tools, "_checkpoint_exists", return_value=True):
            mayor_tools.call_tool("bulldoze_entity", {"entityType": "building", "id": 7, "checkpointName": "cp"})
        self.now += mayor_tools.READ_CACHE_SETTLE_SECONDS
        self._read()
        self.assertEqual(2, self.sent.count(NETWORKS))
        self.assertIn(("POST", "/commands/bulldoze"), self.sent)

    def test_a_preview_keeps_it(self):
        self._paused_and_settled()
        self._read()
        mayor_tools._request("POST", "/commands/preview-network", body={"roadPrefab": "Basic Road"})
        self._read()
        self.assertEqual(1, self.sent.count(NETWORKS))

    def test_nothing_is_kept_while_the_simulation_runs(self):
        self._paused_and_settled()
        mayor_tools.call_tool("set_simulation_speed", {"paused": False, "speed": 3})
        self.now += 60
        self._read()
        self._read()
        self.assertEqual(2, self.sent.count(NETWORKS))

    def test_a_read_right_after_a_change_is_not_kept(self):
        self._paused_and_settled()
        mayor_tools._request("POST", "/commands/bulldoze", body={"id": 1})
        self._read()  # the game may still be finishing the change
        self.now += mayor_tools.READ_CACHE_SETTLE_SECONDS
        self._read()
        self._read()
        self.assertEqual(2, self.sent.count(NETWORKS))

    def test_a_kept_read_expires(self):
        self._paused_and_settled()
        self._read()
        self.now += mayor_tools.READ_CACHE_TTL_SECONDS + 1
        self._read()
        self.assertEqual(2, self.sent.count(NETWORKS))

    def test_small_reads_are_never_kept(self):
        self._paused_and_settled()
        mayor_tools._request("GET", "/state/summary")
        mayor_tools._request("GET", "/state/summary")
        self.assertEqual(2, self.sent.count(("GET", "/state/summary")))


if __name__ == "__main__":
    unittest.main()
