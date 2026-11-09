"""Offline tests for dead-end frontage extension planning; no live game."""

import unittest

from frontage_road import dead_end_anchors, expected_frontage_cells, plan_frontage_extensions

BOUNDS = [(-960.0, -960.0, 960.0, 960.0)]


def road(segment_id, start, end, start_degree=2, end_degree=2, prefab="Gravel Road"):
    dx, dz = end[0] - start[0], end[1] - start[1]
    length = (dx * dx + dz * dz) ** 0.5
    return {
        "id": segment_id, "prefab": prefab,
        "startNodeId": segment_id * 10, "endNodeId": segment_id * 10 + 1,
        "startNodeDegree": start_degree, "endNodeDegree": end_degree,
        "start": {"x": start[0], "z": start[1]}, "end": {"x": end[0], "z": end[1]},
        "startDirection": {"x": dx / length, "z": dz / length},
        "endDirection": {"x": -dx / length, "z": -dz / length},
    }


def accept_all(result):
    return result.get("valid") is True


def preview_ok(candidate):
    return {"valid": True, "constructionCost": 16000, "terrainProfile": {"minimumRoadAboveTerrain": 0.2}}


class FrontageRoadTests(unittest.TestCase):
    def test_dead_end_heading_continues_the_existing_road(self):
        anchors = dead_end_anchors([road(1, (0, 0), (0, 100), end_degree=1)])
        self.assertEqual(1, len(anchors))
        self.assertEqual(11, anchors[0]["nodeId"])
        self.assertAlmostEqual(1.0, anchors[0]["heading"][1])

    def test_highways_and_outside_connections_are_not_anchors(self):
        segments = [road(1, (0, 0), (0, 100), end_degree=1, prefab="Highway"),
                    {**road(2, (500, 0), (500, 100), end_degree=1), "endOutside": True}]
        self.assertEqual([], dead_end_anchors(segments))

    def test_extensions_stay_below_the_bridge_corner_limit(self):
        segments = [road(1, (0, 0), (0, 100), end_degree=1)]
        result = plan_frontage_extensions(
            anchors=dead_end_anchors(segments), segments=segments, bounds=BOUNDS,
            preview=preview_ok, accepted=accept_all, intended_zone="Residential")
        self.assertFalse(result["mutationAuthorized"])
        self.assertTrue(result["candidates"])
        self.assertTrue(all(abs(item["deflectionDegrees"]) < 45 for item in result["candidates"]))
        self.assertEqual({"x": 0.0, "z": 100.0}, result["candidates"][0]["candidate"]["start"])
        # A perfectly straight extension of a dead end would invoke native MoveMiddleNode.
        self.assertTrue(all(item["deflectionDegrees"] != 0 for item in result["candidates"]))

    def test_static_gates_reject_before_native_preview(self):
        segments = [road(1, (0, 0), (0, 100), end_degree=1),
                    road(2, (-200, 160), (200, 160))]
        calls = []
        result = plan_frontage_extensions(
            anchors=dead_end_anchors(segments), segments=segments, bounds=BOUNDS,
            preview=lambda c: calls.append(c) or preview_ok(c), accepted=accept_all,
            intended_zone="Residential", conflicting_points=[(40.0, 150.0)],
            deflections=(0.0,), lengths=(96.0,))
        self.assertEqual(0, len(calls))
        self.assertEqual(1, result["stream"]["generated"])
        self.assertEqual(0, result["stream"]["staticallyEligible"])

    def test_residential_rejects_extension_near_industry(self):
        segments = [road(1, (0, 0), (0, 100), end_degree=1)]
        result = plan_frontage_extensions(
            anchors=dead_end_anchors(segments), segments=segments, bounds=BOUNDS,
            preview=preview_ok, accepted=accept_all, intended_zone="Residential",
            conflicting_points=[(10.0, 150.0)], deflections=(8.0,), lengths=(96.0,))
        self.assertEqual(1, result["rejections"]["nearConflictingUse"])

    def test_preview_rejection_and_budget_are_counted(self):
        segments = [road(1, (0, 0), (0, 100), end_degree=1)]
        result = plan_frontage_extensions(
            anchors=dead_end_anchors(segments), segments=segments, bounds=BOUNDS,
            preview=lambda c: {"valid": False}, accepted=accept_all,
            intended_zone="Residential", maximum_previews=2)
        self.assertEqual(2, result["stream"]["previewed"])
        self.assertEqual(0, result["stream"]["accepted"])
        self.assertEqual(2, result["rejections"]["nativePreviewOrTerrain"])
        self.assertGreater(result["rejections"]["previewBudget"], 0)

    def test_previous_failures_are_excluded(self):
        segments = [road(1, (0, 0), (0, 100), end_degree=1)]
        result = plan_frontage_extensions(
            anchors=dead_end_anchors(segments), segments=segments, bounds=BOUNDS,
            preview=preview_ok, accepted=accept_all, intended_zone="Industrial",
            rejected=lambda candidate: True)
        self.assertEqual(0, result["stream"]["staticallyEligible"])
        self.assertEqual(18, result["rejections"]["previousFailure"])

    def test_expected_cells_cover_both_sides(self):
        self.assertEqual(88, expected_frontage_cells(96.0))


if __name__ == "__main__":
    unittest.main()
