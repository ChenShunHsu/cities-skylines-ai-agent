#!/usr/bin/env python3

import hashlib
import io
import json
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from mayor import transport


class FakeResponse:
    def __init__(self, payload=None, read_error=None):
        self.payload = payload
        self.read_error = read_error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        if self.read_error:
            raise self.read_error
        return json.dumps(self.payload).encode("utf-8")


class RecordingOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class MayorFoundationTests(unittest.TestCase):
    def test_tool_definition_contract_matches_snapshot(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "mayor_tools.py"), "definitions"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(["tools"], list(payload))
        definitions = payload["tools"]
        fixture = json.loads(
            (SCRIPTS / "fixtures" / "mayor-tools-contract-v1.json").read_text(encoding="utf-8")
        )
        canonical = json.dumps(
            definitions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(fixture["toolCount"], len(definitions))
        self.assertEqual(
            fixture["names"], [item["function"]["name"] for item in definitions]
        )
        self.assertEqual(fixture["canonicalSha256"], hashlib.sha256(canonical).hexdigest())

    def test_fake_transport_records_exact_request_without_retry(self):
        opener = RecordingOpener([FakeResponse({"ok": True, "value": 7})])
        result = transport.request(
            "http://bridge.test/",
            "POST",
            "/commands/example",
            query={"enabled": True, "skip": None},
            body={"value": 7},
            timeout=3.5,
            opener=opener,
        )
        self.assertEqual({"ok": True, "value": 7}, result)
        self.assertEqual(1, len(opener.calls))
        request, timeout = opener.calls[0]
        self.assertEqual("http://bridge.test/commands/example?enabled=true", request.full_url)
        self.assertEqual("POST", request.get_method())
        self.assertEqual({"value": 7}, json.loads(request.data.decode("utf-8")))
        self.assertEqual("application/json", request.headers["Content-type"])
        self.assertEqual(3.5, timeout)

    def test_fake_transport_preserves_structured_http_error_without_retry(self):
        payload = {"ok": False, "error": "native rejection", "layer": "preview"}
        failure = urllib.error.HTTPError(
            "http://bridge.test/commands/example",
            409,
            "Conflict",
            {},
            io.BytesIO(json.dumps(payload).encode("utf-8")),
        )
        opener = RecordingOpener([failure, FakeResponse({"ok": True})])
        with self.assertRaises(transport.BridgeRequestError) as raised:
            transport.request(
                "http://bridge.test", "POST", "/commands/example", body={}, opener=opener
            )
        self.assertEqual(payload, raised.exception.detail)
        self.assertEqual(1, len(opener.calls))
        failure.close()

    def test_fake_transport_does_not_retry_timeout_or_interrupted_response(self):
        outcomes = (
            TimeoutError("bridge timed out"),
            FakeResponse(read_error=ConnectionResetError("response interrupted")),
        )
        expected = (TimeoutError, ConnectionResetError)
        for outcome, error_type in zip(outcomes, expected):
            with self.subTest(error=error_type.__name__):
                opener = RecordingOpener([outcome, FakeResponse({"ok": True})])
                with self.assertRaises(error_type):
                    transport.request(
                        "http://bridge.test", "GET", "/state/example", opener=opener
                    )
                self.assertEqual(1, len(opener.calls))

    def test_fake_transport_identifies_unavailable_bridge(self):
        opener = RecordingOpener([urllib.error.URLError("connection refused")])
        with self.assertRaisesRegex(RuntimeError, "unavailable at http://bridge.test"):
            transport.request("http://bridge.test", "GET", "/health", opener=opener)
        self.assertEqual(1, len(opener.calls))



class UnrunResendTests(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        import mayor_tools
        self.tools = mayor_tools
        self.busy = transport.BridgeRequestError(
            {"ok": False, "error": "Timed out waiting for the game thread; the command was not run."})
        sleep = mock.patch.object(mayor_tools.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def send(self, effects, method="POST", path="/commands/bulldoze"):
        from unittest import mock
        with mock.patch.object(self.tools.mayor_transport, "request", side_effect=effects) as sent:
            try:
                return self.tools._request(method, path, body={} if method == "POST" else None), sent.call_count
            except Exception as error:  # noqa: BLE001 - the test reads what was raised
                return error, sent.call_count

    def test_a_change_the_busy_thread_withdrew_unrun_is_sent_again(self):
        self.assertEqual(({"ok": True}, 2), self.send([self.busy, {"ok": True}]))
        self.assertEqual(self.tools.UNRUN_RESEND_SECONDS, self.sleep.call_args.args[0])

    def test_resends_are_bounded(self):
        result, calls = self.send([self.busy] * 5)
        self.assertIsInstance(result, transport.BridgeRequestError)
        self.assertEqual(self.tools.UNRUN_RESENDS + 1, calls)

    def test_other_refusals_and_a_timed_out_change_are_never_sent_again(self):
        refused = transport.BridgeRequestError({"ok": False, "error": "Timed out; its outcome is unknown."})
        self.assertEqual(1, self.send([refused, {"ok": True}])[1])
        result, calls = self.send([TimeoutError("timed out"), {"ok": True}])
        self.assertEqual((TimeoutError, 1), (type(result), calls))

    def test_a_change_that_found_no_free_socket_is_sent_again_and_bounded(self):
        no_socket = RuntimeError("Cities: Skylines Agent Bridge is unavailable at http://127.0.0.1:32123: "
                                 "[WinError 10055] no buffer space")
        self.assertEqual(({"ok": True}, 2), self.send([no_socket, {"ok": True}]))
        self.assertEqual(self.tools.NO_SOCKET_SECONDS, self.sleep.call_args.args[0])
        result, calls = self.send([no_socket] * 6)
        self.assertEqual((RuntimeError, self.tools.NO_SOCKET_RESENDS + 1), (type(result), calls))
        down = RuntimeError("Cities: Skylines Agent Bridge is unavailable at http://127.0.0.1:32123: refused")
        self.assertEqual(1, self.send([down, {"ok": True}])[1])  # a Bridge that is down is not waited on

    def test_a_read_that_timed_out_is_asked_once_more(self):
        self.assertEqual(({"ok": True}, 2), self.send([TimeoutError("timed out"), {"ok": True}], "GET", "/state/summary"))
        result, calls = self.send([TimeoutError("timed out")] * 3, "GET", "/state/summary")
        self.assertEqual((TimeoutError, 2), (type(result), calls))


class SameThreadRetryTests(unittest.TestCase):
    def test_a_read_is_asked_again_once_and_a_change_never(self):
        from unittest import mock
        import mayor_tools
        busy = transport.BridgeRequestError(
            {"ok": False, "error": "InvalidOperationException: Already in the same thread. Call directly"})
        with mock.patch.object(mayor_tools.mayor_transport, "request", side_effect=[busy, {"ok": True}]) as sent:
            self.assertEqual({"ok": True}, mayor_tools._request("POST", "/commands/preview-placement", body={}))
        self.assertEqual(2, sent.call_count)
        with mock.patch.object(mayor_tools.mayor_transport, "request", side_effect=[busy, {"ok": True}]) as sent:
            with self.assertRaises(transport.BridgeRequestError):
                mayor_tools._request("POST", "/commands/build-network", body={})
        self.assertEqual(1, sent.call_count)


if __name__ == "__main__":
    unittest.main()
