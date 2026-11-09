import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mayor import land_use  # noqa: E402


def building(service, x, z):
    return {"service": service, "position": {"x": x, "z": z}}


def block(x, z, prefab="Basic Road", segment=1):
    return {"center": {"x": x, "z": z}, "roadPrefab": prefab, "roadSegmentId": segment}


HOMES = [building("Residential", x, z) for x in (0, 40, 80) for z in (0, 40)]
FACTORIES = [building("Industrial", 1000 + x, z) for x in (0, 40, 80) for z in (0, 40)]


class GroundTests(unittest.TestCase):
    def sample(self, x):
        return {"position": {"x": x, "z": 0}}

    def test_homes_get_the_part_of_an_area_out_of_industrys_reach(self):
        factory = [{"service": "Industrial", "position": {"x": 0, "z": 0}}]
        ground = land_use.ground_for("residential", [self.sample(x) for x in (100, 230, 250, 400, 900)], factory)
        # 240m: as far as industry's ground pollution was seen to reach homes
        self.assertEqual([250, 400, 900], [s["position"]["x"] for s in ground])

    def test_new_industry_keeps_out_of_the_homes_reach_unless_planned(self):
        homes = [{"service": "Residential", "position": {"x": 0, "z": 0}}]
        plan = [{"kind": "workplace", "minX": 50, "maxX": 150, "minZ": -10, "maxZ": 10}]
        ground = land_use.ground_for("workplace", [self.sample(x) for x in (100, 200, 400)], homes, plan=plan)
        self.assertEqual([100, 400], [s["position"]["x"] for s in ground])


class IndustryReachTests(unittest.TestCase):
    def test_an_industrial_district_does_not_extend_into_its_pollutions_reach_of_homes(self):
        industry = [{"service": "Industrial", "position": {"x": x, "z": 0}} for x in (0, 30, 60)]
        near_home = industry + [{"service": "Residential", "position": {"x": 300, "z": 0}}]
        far_home = industry + [{"service": "Residential", "position": {"x": 380, "z": 0}}]
        block = {"center": {"x": 120, "z": 0}}
        self.assertNotIn("workplace", land_use.classify(block, near_home)["allowed"])  # 180m from a home
        self.assertIn("workplace", land_use.classify(block, far_home)["allowed"])  # 260m


class ZonedLandTests(unittest.TestCase):
    def test_land_zoned_but_not_built_keeps_the_gap(self):
        blocks = [{"zone": "Industrial", "zonedCells": 12, "center": {"x": 0, "z": 0}},
                  {"zone": "ResidentialLow", "zonedCells": 8, "center": {"x": 2000, "z": 0}},
                  {"zone": "Unzoned", "zonedCells": 0, "center": {"x": 210, "z": 0}}]
        zoned = land_use.zoned_land(blocks)
        self.assertEqual(["workplace", "residential"], [land["use"] for land in zoned])
        homes = {"center": {"x": 210, "z": 0}}
        self.assertIn("residential", land_use.classify(homes, [])["allowed"])
        self.assertNotIn("residential", land_use.classify(homes, [], zoned=zoned)["allowed"])
        factory = {"center": {"x": 1850, "z": 0}}
        self.assertNotIn("workplace", land_use.classify(factory, [], zoned=zoned)["allowed"])
        samples = [{"position": {"x": x, "z": 0}} for x in (210, 900)]
        self.assertEqual([900], [s["position"]["x"] for s in land_use.ground_for("residential", samples, [], zoned=zoned)])
        # zoned land is no neighbour: it does not make a quiet block an industrial district
        self.assertEqual("no neighbours: residential unless industry is near",
                         land_use.classify({"center": {"x": 900, "z": 0}}, [], zoned=zoned)["reason"])


class NoiseTests(unittest.TestCase):
    def test_no_homes_inside_a_noisy_facilitys_radius(self):
        block = {"center": {"x": 0, "z": 0}}
        turbine = [{"position": {"x": 100, "z": 0}, "pollutionRadius": 0, "noiseRadius": 150}]
        far = [{"position": {"x": 400, "z": 0}, "pollutionRadius": 0, "noiseRadius": 150}]
        self.assertNotIn("residential", land_use.classify(block, [], polluters=turbine)["allowed"])
        self.assertIn("residential", land_use.classify(block, [], polluters=far)["allowed"])

    def test_a_zone_blocks_far_cells_count_against_the_noise_radius(self):
        turbine = [{"position": {"x": 168, "z": 0}, "pollutionRadius": 0, "noiseRadius": 150}]
        zone_block = {"blockId": 3265, "rows": 6, "center": {"x": 0, "z": 0}}
        self.assertNotIn("residential", land_use.classify(zone_block, [], polluters=turbine)["allowed"])
        beyond = [{"position": {"x": 200, "z": 0}, "pollutionRadius": 0, "noiseRadius": 150}]
        self.assertIn("residential", land_use.classify(zone_block, [], polluters=beyond)["allowed"])


class LandUseTests(unittest.TestCase):
    def test_block_takes_its_neighbourhood_use(self):
        verdict = land_use.classify(block(40, 120), HOMES + FACTORIES)
        self.assertEqual("residential", verdict["preferred"])
        self.assertNotIn("workplace", verdict["allowed"])  # homes are within 300m

    def test_industry_extends_an_industrial_district_only(self):
        verdict = land_use.classify(block(1040, 120), HOMES + FACTORIES)
        self.assertEqual("workplace", verdict["preferred"])
        self.assertNotIn("residential", verdict["allowed"])
        self.assertNotIn("commercial", verdict["allowed"])  # no shops inside an industrial district

    def test_homes_keep_clear_of_polluting_facilities(self):
        verdict = land_use.classify(block(40, 120), HOMES,
                                    polluters=[{"position": {"x": 40, "z": 300}, "pollutionRadius": 150}])
        self.assertNotIn("residential", verdict["allowed"])

    def test_buffer_between_industry_and_homes_is_commercial(self):
        homes = [building("Residential", 0, 0)]
        factories = [building("Industrial", 400, 0)]
        verdict = land_use.classify(block(200, 0), homes + factories)
        self.assertEqual(["commercial", "office"], verdict["allowed"])
        self.assertEqual("commercial", verdict["preferred"])
        self.assertEqual("buffer between industry and homes", verdict["reason"])

    def test_offices_may_take_the_land_industry_keeps_homes_off_but_not_an_industrial_district(self):
        homes = [building("Residential", 0, 0)]
        factories = [building("Industrial", 400, 0)]
        self.assertIn("office", land_use.classify(block(200, 0), homes + factories)["allowed"])  # the buffer
        self.assertNotIn("office", land_use.classify(block(-600, 0), homes + factories)["allowed"])  # homes' land
        district = land_use.classify(block(1040, 60), FACTORIES)
        self.assertNotIn("office", district["allowed"])  # industry keeps its own district
        self.assertIn("workplace", district["allowed"])

    def test_offices_keep_no_gap_from_homes(self):
        offices = [building("Office", 400, 0), building("Office", 440, 0)]
        self.assertIn("residential", land_use.classify(block(250, 0), offices)["allowed"])
        zoned = [{"use": "workplace", "zone": "Office", "position": {"x": 300, "z": 0}}]
        self.assertIn("residential", land_use.classify(block(250, 0), [], zoned=zoned)["allowed"])
        samples = [{"position": {"x": 250, "z": 0}}]
        self.assertEqual(samples, land_use.ground_for("residential", samples, offices, zoned=zoned))
        # industry still keeps them apart
        factories = [building("Industrial", 400, 0)]
        self.assertNotIn("residential", land_use.classify(block(250, 0), factories)["allowed"])
        zoned = [{"use": "workplace", "zone": "Industrial", "position": {"x": 300, "z": 0}}]
        self.assertNotIn("residential", land_use.classify(block(250, 0), [], zoned=zoned)["allowed"])
        self.assertEqual([], land_use.ground_for("residential", samples, factories))

    def test_shops_on_through_roads_and_arterials_not_on_branches(self):
        through = land_use.classify(block(40, 120, segment=7), HOMES, commercial_blocks=[(40, 200)],
                                    through_roads={7})
        branch = land_use.classify(block(40, 120, segment=8), HOMES, commercial_blocks=[(40, 200)],
                                   through_roads={7})
        arterial = land_use.classify(block(40, 120, prefab="Small 4 Lane Road", segment=8), HOMES,
                                     commercial_blocks=[(40, 200)], through_roads={7})
        self.assertIn("commercial", through["allowed"])
        self.assertNotIn("commercial", branch["allowed"])  # a shop already within 250m
        self.assertIn("commercial", arterial["allowed"])

    def test_plan_overrides_the_neighbourhood(self):
        verdict = land_use.classify(block(40, 120), HOMES,
                                    plan=[{"minX": 0, "maxX": 100, "minZ": 100, "maxZ": 200, "kind": "commercial"}])
        self.assertEqual(["commercial"], verdict["allowed"])

    def test_new_land_is_residential_unless_industry_is_near(self):
        self.assertEqual("residential", land_use.classify(block(5000, 5000), HOMES)["preferred"])
        self.assertEqual("workplace", land_use.classify(block(1200, 200), FACTORIES)["preferred"])
        self.assertEqual("residential", land_use.classify(block(1200, 300), FACTORIES)["preferred"])  # 286m off

    def test_through_segments_exclude_dead_end_chains(self):
        def road(segment, a, b):
            return {"id": segment, "startNodeId": a, "endNodeId": b}
        # junctions 1 and 4 joined by 1-2-3-4; a dead-end branch 4-5-6; a third road at each junction
        roads = [road(1, 1, 2), road(2, 2, 3), road(3, 3, 4), road(4, 4, 5), road(5, 5, 6),
                 road(6, 1, 7), road(7, 1, 8), road(8, 4, 9)]
        through = land_use.through_segments(roads)
        self.assertTrue({1, 2, 3} <= through)
        self.assertFalse({4, 5} & through)

    def test_town_center_is_the_median_home(self):
        self.assertEqual((40, 40), land_use.town_center(HOMES + FACTORIES))


class WellTests(unittest.TestCase):
    WELL = [(0.0, 0.0)]

    def test_industry_keeps_off_a_groundwater_pump_homes_need_not(self):
        near = {"blockId": 1, "rows": 4, "center": {"x": 280, "z": 0}}  # its far cells reach 256m
        far = {"blockId": 2, "rows": 4, "center": {"x": 340, "z": 0}}
        verdict = land_use.classify(near, [], wells=self.WELL)
        self.assertNotIn("workplace", verdict["allowed"])
        self.assertIn("residential", verdict["allowed"])
        self.assertIn("workplace", land_use.classify(far, [], wells=self.WELL)["allowed"])

    def test_new_industrial_ground_leaves_the_pumps_clearance(self):
        samples = [{"position": {"x": x, "z": 0}} for x in (100, 250, 350)]
        self.assertEqual([350], [s["position"]["x"] for s in
                                 land_use.ground_for("workplace", samples, [], wells=self.WELL)])
        self.assertEqual(3, len(land_use.ground_for("residential", samples, [], wells=self.WELL)))


if __name__ == "__main__":
    unittest.main()
