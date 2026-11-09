import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mayor_tools as tools
from road_attempts import (
    geometry_key,
    is_rejected,
    load_failures,
    record_failure,
    recovery_verified,
    rejected_keys,
)


class RoadAttemptTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "failures.json"
        patch = mock.patch.object(tools, "ROAD_FAILURE_PATH", self.path)
        patch.start()
        self.addCleanup(patch.stop)
        self.city = {"city": {"instanceId": "test-city", "name": "Test", "map": "Test"}}
        self.candidate = {"roadPrefab": "Basic Road", "start": {"x": 0, "z": 0}, "end": {"x": 64, "z": 0}}

    def settled(self, **overrides):
        result = {
            "ok": True,
            "segmentId": 42,
            "settledVerification": True,
            "terrainProfile": {
                "source": "nativeGenerateBezierAndLiveTerrain",
                "verificationStage": "settled",
                "clearanceModel": "legacyNodeInsetProxyNotCalibratedSurface",
                "required": True,
                "sampleCount": 9,
                "maxRoadToTerrainDelta": 0.1,
                "minimumRoadAboveTerrain": 0.1,
                "maximumRoadAboveTerrain": 0.2,
                "maxLocalTerrainGrade": 0.01,
                "maximumRoadToTerrainDelta": 1.0,
                "minimumRoadAboveTerrainAllowed": 0.02,
                "maximumLocalTerrainGrade": 0.25,
                "originalNodeTransition": False,
                "safe": True,
            },
        }
        result.update(overrides)
        return result

    def immediate(self, required=True):
        profile = dict(self.settled()["terrainProfile"])
        profile["verificationStage"] = "immediate"
        profile["required"] = required
        if not required:
            profile["sampleCount"] = 0
        return profile

    def test_iterative_step_rejects_live_reversing_turn_despite_shorter_distance(self):
        candidate = {"start": {"x": 822.61, "z": 165.018},
                     "end": {"x": 885.184057, "z": 170.966128}}
        other = {"position": {"x": 879.376, "z": 192.009},
                 "direction": {"x": -0.5, "z": -0.8660254}}
        self.assertFalse(tools._iterative_pair_remains_plannable(candidate, other, 256))

    def test_iterative_pair_preserves_direction_and_search_radius(self):
        other = {"position": {"x": 0, "z": 40}, "direction": {"x": 1, "z": 0}}
        candidate = {"start": {"x": 0, "z": 0}, "end": {"x": 64, "z": 0}}
        self.assertTrue(tools._iterative_pair_remains_plannable(candidate, other, 256))
        self.assertFalse(tools._iterative_pair_remains_plannable(candidate, other, 48))
        for end in ({"x": 0, "z": 0}, {"x": 16, "z": 0}, {"x": float("nan"), "z": 0}):
            self.assertFalse(tools._iterative_pair_remains_plannable({**candidate, "end": end}, other, 256))

    def test_keys_match_reversed_geometry_and_different_prefab(self):
        other = {"roadPrefab": "Gravel Road", "start": self.candidate["end"], "end": self.candidate["start"]}
        self.assertEqual(geometry_key(self.candidate), geometry_key(other))
        self.assertNotEqual(geometry_key(self.candidate), geometry_key({**other, "start": {"x": 65, "z": 0}}))

    def test_failure_is_scoped_to_city_instance_not_map_name(self):
        record_failure(self.path, load_failures(self.path, "first"), self.candidate, {"error": "terrain"})
        self.assertEqual(1, len(load_failures(self.path, "first")["failures"]))
        self.assertEqual([], load_failures(self.path, "second")["failures"])

    def test_atomic_write_failure_preserves_old_history(self):
        record_failure(self.path, load_failures(self.path, "first"), self.candidate, {"error": "first"})
        original = self.path.read_bytes()
        with mock.patch("road_attempts.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                record_failure(self.path, load_failures(self.path, "first"), self.candidate, {"error": "second"})
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual([self.path], list(self.path.parent.iterdir()))

    def test_invalid_history_is_not_silently_discarded(self):
        self.path.write_text('{"version":1,"instanceId":"test-city","failures":[null]}')
        with self.assertRaises(ValueError):
            load_failures(self.path, "test-city")

    def test_history_is_bounded_and_deduplicated(self):
        for index in range(105):
            candidate = {**self.candidate, "end": {"x": index + 1, "z": 0}}
            record_failure(self.path, load_failures(self.path, "test-city"), candidate, {})
        state = load_failures(self.path, "test-city")
        self.assertEqual(100, len(state["failures"]))
        record_failure(self.path, state, candidate, {"error": "again"})
        self.assertEqual(100, len(load_failures(self.path, "test-city")["failures"]))

    def test_invalid_candidate_or_mismatched_key_blocks_history(self):
        for candidate in ({}, {"start": None}, {**self.candidate, "end": {"x": 999, "z": 0}}):
            with self.subTest(candidate=candidate):
                self.path.write_text(json.dumps({"version": 1, "instanceId": "test-city", "failures": [
                    {"key": geometry_key(self.candidate), "candidate": candidate, "evidence": {}}
                ]}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_failures(self.path, "test-city")

    def test_a_failure_under_older_road_gates_no_longer_blocks_the_alignment(self):
        record_failure(self.path, load_failures(self.path, "test-city"), self.candidate, {})
        self.assertTrue(is_rejected(self.candidate, load_failures(self.path, "test-city")))
        state = json.loads(self.path.read_text(encoding="utf-8"))
        del state["failures"][0]["gatePolicy"]  # written before the gates were versioned
        self.path.write_text(json.dumps(state), encoding="utf-8")
        self.assertFalse(is_rejected(self.candidate, load_failures(self.path, "test-city")))

    def test_near_identical_retry_is_excluded_but_distinct_alignment_is_not(self):
        record_failure(self.path, load_failures(self.path, "test-city"), self.candidate, {})
        state = load_failures(self.path, "test-city")
        self.assertTrue(is_rejected({**self.candidate, "end": {"x": 65.4, "z": 0.5}}, state))
        self.assertFalse(is_rejected({**self.candidate, "end": {"x": 64, "z": 24}}, state))

    def test_a_cut_is_not_a_retry_of_a_raw_clearance_refusal_only(self):
        refused = {"ok": False, "recovery": {"noConstructionAttempted": True}}
        built_and_rolled_back = {"ok": False, "recovery": {"localRollbackSucceeded": True}}
        cut = {**self.candidate, "allowTerrainCut": True}
        for evidence, previous, rejected in ((refused, self.candidate, False),
                                             (built_and_rolled_back, self.candidate, True),
                                             (refused, cut, True)):
            with self.subTest(evidence=evidence, previous=previous):
                self.path.unlink(missing_ok=True)
                record_failure(self.path, load_failures(self.path, "test-city"), previous, evidence)
                self.assertEqual(rejected, is_rejected(cut, load_failures(self.path, "test-city")))
        self.assertTrue(is_rejected(self.candidate, load_failures(self.path, "test-city")))

    def test_checkpoint_restore_can_verify_recovery_without_claiming_local_rollback(self):
        entry = {"evidence": {"recovery": {
            "localRollbackSucceeded": False,
            "checkpointRestoreRequired": True,
            "checkpointRestoreVerified": True,
            "segmentAbsent": True,
            "regressionBaselineName": "before-road",
        }}}
        self.assertTrue(recovery_verified(entry))
        entry["evidence"]["recovery"]["checkpointRestoreVerified"] = False
        self.assertFalse(recovery_verified(entry))

    def test_build_waits_until_pending_verification_finishes(self):
        build = {"ok": True, "segmentId": 42, "verificationPending": True,
                 "actualTerrainProfile": self.immediate()}
        pending = {"ok": True, "verificationPending": True}
        responses = [self.city, build, {}, pending, {}, pending, {}, self.settled()]
        with mock.patch.object(tools, "_request", side_effect=responses) as request, mock.patch.object(tools.time, "sleep"):
            result = tools._build_verified_network(self.candidate)
        self.assertTrue(result["ok"])
        self.assertFalse(result["verificationPending"])
        self.assertEqual(3, sum(call.args[1] == "/commands/finalize-road-build" for call in request.call_args_list))
        self.assertEqual([], load_failures(self.path, "test-city")["failures"])

    BUSY = {"ok": False, "error": "Timed out waiting for the game thread; the command was not run."}

    def test_a_settlement_check_the_busy_game_thread_did_not_run_is_sent_again(self):
        build = {"ok": True, "segmentId": 42, "verificationPending": True, "actualTerrainProfile": self.immediate()}
        responses = [self.city, build, tools.BridgeRequestError(self.BUSY), {},
                     tools.BridgeRequestError(self.BUSY), self.settled()]
        with mock.patch.object(tools, "_request", side_effect=responses), mock.patch.object(tools.time, "sleep"):
            result = tools._build_verified_network(self.candidate)
        self.assertTrue(result["ok"])
        self.assertEqual([], load_failures(self.path, "test-city")["failures"])

    def test_a_thread_busy_at_every_try_still_leaves_the_road_unverified(self):
        build = {"ok": True, "segmentId": 42, "verificationPending": True, "actualTerrainProfile": self.immediate()}
        responses = [self.city, build] + [tools.BridgeRequestError(self.BUSY)] * tools.SETTLE_BUSY_TRIES
        with mock.patch.object(tools, "_request", side_effect=responses), mock.patch.object(tools.time, "sleep"):
            with self.assertRaises(tools.BridgeRequestError):
                tools._build_verified_network(self.candidate)
        entry = load_failures(self.path, "test-city")["failures"][0]
        self.assertFalse(recovery_verified(entry))
        self.assertEqual(42, entry["evidence"]["buildResult"]["segmentId"])

    def record_unsettled(self, error=None):
        record_failure(self.path, load_failures(self.path, "test-city"), self.candidate, {
            "ok": False, "error": error or self.BUSY["error"],
            "buildResult": {"ok": True, "segmentId": 42, "verificationPending": True},
            "recovery": {"localRollbackSucceeded": False, "checkpointRestoreRequired": True}})

    def test_a_road_built_but_never_settled_is_settled_later_and_unblocks_roads(self):
        self.record_unsettled()
        with mock.patch.object(tools, "_request", side_effect=[{}, self.settled()]) as request, \
                mock.patch.object(tools.time, "sleep"):
            history = tools._road_failures({"instanceId": "test-city"})
        self.assertEqual([], history["failures"])
        self.assertEqual([], load_failures(self.path, "test-city")["failures"])
        self.assertEqual({"segmentId": 42}, request.call_args_list[1].kwargs["body"])

    def test_a_road_that_will_not_settle_or_failed_otherwise_still_blocks(self):
        self.record_unsettled()
        gone = {"ok": False, "error": "Segment 42 does not exist."}
        with mock.patch.object(tools, "_request", side_effect=[{}, tools.BridgeRequestError(gone)]), \
                mock.patch.object(tools.time, "sleep"):
            self.assertEqual(1, len(tools._road_failures({"instanceId": "test-city"})["failures"]))
        self.record_unsettled("Road settlement did not finish")  # not a busy thread: never re-checked
        with mock.patch.object(tools, "_request", side_effect=AssertionError("no request")):
            self.assertEqual(1, len(tools._road_failures({"instanceId": "test-city"})["failures"]))

    def test_settled_terrain_contract_fails_closed(self):
        baseline = self.settled()
        corrupt = [
            {**baseline, "ok": 1},
            {**baseline, "segmentId": 43},
            {**baseline, "settledVerification": 1},
            {**baseline, "terrainProfile": None},
            {**baseline, "terrainProfile": {
                **baseline["terrainProfile"], "verificationStage": "immediate"}},
            {**baseline, "terrainProfile": {
                **baseline["terrainProfile"], "clearanceModel": "terrainSeededControlCurve"}},
            {**baseline, "terrainProfile": {
                **baseline["terrainProfile"], "sampleCount": True}},
            {**baseline, "terrainProfile": {
                **baseline["terrainProfile"], "minimumRoadAboveTerrain": 0.01}},
            {**baseline, "terrainProfile": {
                **baseline["terrainProfile"], "maxRoadToTerrainDelta": 1.1}},
            {**baseline, "terrainProfile": {
                **baseline["terrainProfile"], "maxLocalTerrainGrade": 0.3}},
        ]
        for response in corrupt:
            with self.subTest(response=response):
                with self.assertRaises(RuntimeError):
                    tools._validated_settled_road_terrain_profile(response, 42)

    def test_ground_build_cannot_bypass_settlement_with_false_pending_flag(self):
        build = {"ok": True, "segmentId": 42, "verificationPending": False,
                 "actualTerrainProfile": self.immediate()}
        with mock.patch.object(tools, "_request", side_effect=[self.city, build]):
            with self.assertRaises(tools.BridgeRequestError) as raised:
                tools._build_verified_network(self.candidate)
        self.assertIn("disagrees", raised.exception.detail["error"])

    def test_non_ground_build_requires_typed_immediate_profile_without_settlement(self):
        build = {"ok": True, "segmentId": 42, "verificationPending": False,
                 "actualTerrainProfile": self.immediate(required=False)}
        with mock.patch.object(tools, "_request", side_effect=[self.city, build]):
            result = tools._build_verified_network(self.candidate)
        self.assertTrue(result["ok"])
        self.assertEqual([], load_failures(self.path, "test-city")["failures"])

    def test_http_finalize_failure_preserves_creation_and_recovery_evidence(self):
        build = {"ok": True, "segmentId": 42, "verificationPending": True,
                 "actualTerrainProfile": self.immediate(),
                 "geometryEvidence": {"center": {"a": {"x": 0}}}}
        failed = {"ok": False, "error": "terrain mismatch", "recovery": {"localRollbackSucceeded": True}, "settledGeometryEvidence": {"samples": [0.0045]}}
        with mock.patch.object(tools, "_request", side_effect=[self.city, build, {}, tools.BridgeRequestError(failed)]):
            with self.assertRaises(tools.BridgeRequestError) as raised:
                tools._build_verified_network(self.candidate, "checkpoint-test")
        detail = raised.exception.detail
        self.assertEqual(42, detail["buildResult"]["segmentId"])
        self.assertTrue(detail["recovery"]["localRollbackSucceeded"])
        self.assertEqual("checkpoint-test", detail["candidate"]["checkpointName"])
        self.assertIn(geometry_key(self.candidate), rejected_keys(load_failures(self.path, "test-city")))

    def test_validation_refusal_before_creation_does_not_block_other_roads(self):
        refused = {"ok": False, "error": "Road extension would invoke CS1's native MoveMiddleNode behavior."}
        with mock.patch.object(tools, "_request", side_effect=[self.city, tools.BridgeRequestError(refused)]):
            with self.assertRaises(tools.BridgeRequestError):
                tools._build_verified_network(self.candidate, "checkpoint-test")
        state = load_failures(self.path, "test-city")
        self.assertTrue(state["failures"][0]["evidence"]["recovery"]["noConstructionAttempted"])
        self.assertTrue(recovery_verified(state["failures"][0]))
        self.assertIn(geometry_key(self.candidate), rejected_keys(state))

    def test_game_thread_timeout_still_requires_recovery(self):
        timeout = {"ok": False, "error": "Timed out waiting for the game thread."}
        with mock.patch.object(tools, "_request", side_effect=[self.city, tools.BridgeRequestError(timeout)]):
            with self.assertRaises(tools.BridgeRequestError):
                tools._build_verified_network(self.candidate, "checkpoint-test")
        state = load_failures(self.path, "test-city")
        self.assertFalse(recovery_verified(state["failures"][0]))

    def test_withdrawn_timeout_is_known_to_have_built_nothing(self):
        # The Bridge now withdraws a timed-out command that never started; one still running
        # keeps "Timed out" in its message and stays unverified.
        withdrawn = {"ok": False, "error": "Timed out waiting for the game thread; the command was not run."}
        with mock.patch.object(tools, "_request", side_effect=[self.city, tools.BridgeRequestError(withdrawn)]):
            with self.assertRaises(tools.BridgeRequestError):
                tools._build_verified_network(self.candidate, "checkpoint-test")
        state = load_failures(self.path, "test-city")
        self.assertTrue(recovery_verified(state["failures"][0]))
        running = {"ok": False, "error": "Timed out while the game thread is still running the command; its outcome is unknown."}
        other = {**self.candidate, "end": {"x": 0, "z": 64}}
        with mock.patch.object(tools, "_request", side_effect=[self.city, tools.BridgeRequestError(running)]):
            with self.assertRaises(tools.BridgeRequestError):
                tools._build_verified_network(other, "checkpoint-test")
        self.assertFalse(recovery_verified(load_failures(self.path, "test-city")["failures"][-1]))

    def test_failed_alignment_is_rejected_before_another_mutation(self):
        record_failure(self.path, load_failures(self.path, "test-city"), self.candidate,
                       {"recovery": {"localRollbackSucceeded": True}})
        with mock.patch.object(tools, "_request", return_value=self.city) as request:
            with self.assertRaisesRegex(RuntimeError, "already failed"):
                tools._build_verified_network(self.candidate)
        self.assertEqual(1, request.call_count)

    def test_crash_leaves_inflight_evidence_and_blocks_new_geometry(self):
        with mock.patch.object(tools, "_request", side_effect=[self.city, KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):
                tools._build_verified_network(self.candidate, "checkpoint-test")
        state = load_failures(self.path, "test-city")
        self.assertFalse(state["failures"][0]["evidence"]["recovery"]["localRollbackSucceeded"])
        other = {**self.candidate, "end": {"x": 70, "z": 1}}
        with mock.patch.object(tools, "_request", return_value=self.city) as request:
            with self.assertRaisesRegex(RuntimeError, "recovery is unverified"):
                tools._build_verified_network(other)
        self.assertEqual(1, request.call_count)

    def test_pending_timeout_cannot_be_reported_as_success(self):
        responses = [self.city, {"ok": True, "segmentId": 42, "verificationPending": True,
                                 "actualTerrainProfile": self.immediate()}]
        responses += [{}, {"ok": True, "verificationPending": True}] * 8
        with mock.patch.object(tools, "_request", side_effect=responses), mock.patch.object(tools.time, "sleep"):
            with self.assertRaises(tools.BridgeRequestError) as raised:
                tools._build_verified_network(self.candidate)
        self.assertTrue(raised.exception.detail["recovery"]["checkpointRestoreRequired"])

    def test_short_native_merge_risk_is_never_accepted(self):
        self.assertFalse(tools._road_preview_survives_settlement({"valid": True, "shortCollinearMergeRisk": True}, 0.7))
        self.assertFalse(tools._road_preview_survives_settlement({"valid": True, "length": 24}, 0.7))

    def test_invalid_native_length_is_rejected_without_crashing_search(self):
        for length in (None, True, "32", float("inf"), float("nan"), -1):
            with self.subTest(length=length):
                self.assertFalse(tools._road_preview_survives_settlement({"valid": True, "length": length}, 0.7))

    def test_terrain_fitting_is_bounded_and_does_not_move_existing_nodes(self):
        source = (tools.ROOT / "src/RoadTerrainFitting.cs").read_text(encoding="utf-8")
        self.assertIn("MaximumNewEndpointTerrainFit = 0.3f", source)
        self.assertIn("if (startNode == 0 && !planningStart) candidateStart.y += lift", source)
        self.assertIn("if (endNode == 0 && !planningEnd) candidateEnd.y += lift", source)
        self.assertIn("ValidateRoadTerrainProfile", source)
        self.assertIn("if (safe)", source)
        self.assertIn("endLift = endNode == 0 && !planningEnd ? lift : 0f;\n                    return;", source)
        self.assertNotIn("bestSafeClearance", source)

    def test_failed_planning_diagnostics_keep_gates_without_large_curve_samples(self):
        preview = {"valid": False, "terrainProfile": {"safe": False},
                   "nativeMoveMiddleNodeRisk": True, "toolErrors": "ObjectCollision",
                   "predictionGeometryEvidence": {"samples": [0] * 129}}
        attempt = {"segment": self.candidate, "preview": preview, "previews": [preview]}
        compact = tools._compact_road_attempt(attempt)
        self.assertEqual(self.candidate, compact["segment"])
        self.assertEqual({"safe": False}, compact["preview"]["terrainProfile"])
        self.assertTrue(compact["preview"]["nativeMoveMiddleNodeRisk"])
        self.assertNotIn("predictionGeometryEvidence", compact["previews"][0])
        self.assertIn("predictionGeometryEvidence", preview)

    def test_native_geometry_contract_preserves_raw_and_proxy_separately(self):
        source = (tools.ROOT / "src/RoadGeometryEvidence.cs").read_text(encoding="utf-8")
        self.assertIn("out left, out right", source)
        self.assertIn("segment.Info.m_surfaceLevel", source)
        self.assertIn("nativeClearance", source)
        self.assertIn("legacyProxyClearance", source)
        self.assertIn('value.ToString("R", CultureInfo.InvariantCulture)', source)

    def test_preview_and_live_profiles_share_dense_sampling_policy(self):
        roads = (tools.ROOT / "src/RoadCommands.cs").read_text(encoding="utf-8")
        geometry = (tools.ROOT / "src/RoadGeometryEvidence.cs").read_text(encoding="utf-8")
        self.assertIn("GeometrySampleCount(predicted)", roads)
        self.assertIn("GeometrySampleCount(curve)", roads)
        self.assertIn("NetSegment.CalculateMiddlePoints", geometry)
        self.assertIn("9, 129", geometry)
        self.assertNotIn("Vector3.Lerp(start, end, t)", roads)

    def test_settlement_has_bounded_stability_gate_and_unchanged_clearance_policy(self):
        roads = (tools.ROOT / "src/RoadCommands.cs").read_text(encoding="utf-8")
        self.assertIn("(pending.StableReads < 3 || (pending.TerrainCut && !cutCarved)) && pending.SettlementReads < 8",
                      roads)
        self.assertIn("GroundRoadMinimumTerrainClearance = 0.02f", roads)
        # leaving a road to the game is opt-in, and refused after settlement only when the drawn
        # terrain covers it, as a player's road would be
        cut = (tools.ROOT / "src/RoadTerrainCut.cs").read_text(encoding="utf-8")
        self.assertIn("FinalSurfaceTolerance = 0.15f", cut)
        self.assertIn("return buried <= FinalSurfaceTolerance;", cut)
        self.assertIn("!(allowTerrainCut && terrainCarvable)", roads)
        self.assertIn("TerrainCut = allowTerrainCut && terrainCarvable,", roads)
        self.assertIn("GroundRoadTerrainDeviationLimit = 1f", roads)
        self.assertIn("WithGeometryEvidence(failure, pending.ImmediateGeometry, settledGeometry)", roads)
        # a neighbour already beyond its limit regresses only when it gets worse than its own baseline
        self.assertIn(": !junctionTransitionSafe && worseThanBaseline;", roads)
        self.assertIn("AdjacentRegressionTolerance = 0.05f", roads)
        # and only when the drawn terrain covers it, not merely the raw heightmap
        self.assertIn("RoadTerrainCut.BuriedDepth(manager, segmentId) <= RoadTerrainCut.FinalSurfaceTolerance", roads)


if __name__ == "__main__":
    unittest.main()
