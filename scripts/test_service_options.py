import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mayor_tools  # noqa: E402
from mayor import service_options  # noqa: E402


def asset(name, service, ai_type, cost, upkeep, **fields):
    return {"name": name, "service": service, "unlocked": True, "constructionCost": cost,
            "maintenanceCost": upkeep, "width": 4, "length": 4,
            "aiProfile": {"aiType": ai_type, "fields": fields}}


LANDFILL = asset("Landfill", "Garbage", "LandfillSiteAI", 400, 100, garbageTruckCount=15,
                 garbageCapacity=8000000, garbageConsumption=0, collectRadius=2000,
                 pollutionAccumulation=100, pollutionRadius=150)
INCINERATOR = asset("Incinerator", "Garbage", "LandfillSiteAI", 3000, 900, garbageTruckCount=27,
                    garbageCapacity=400000, garbageConsumption=3000, electricityProduction=750,
                    collectRadius=2000, noiseAccumulation=50, noiseRadius=150)
FIREHOUSE = asset("Firehouse", "FireDepartment", "FireStationAI", 1200, 350, fireTruckCount=6,
                  fireDepartmentRadius=700)
FIRE_STATION = asset("FireStation", "FireDepartment", "FireStationAI", 6000, 900, fireTruckCount=25,
                     fireDepartmentRadius=1200)
CEMETERY = asset("Cemetery", "HealthCare", "CemeteryAI", 800, 100, hearseCount=10, corpseCapacity=150,
                 burialRate=125, graveCount=3000, deathCareRadius=1000)
CREMATORY = asset("Crematory", "HealthCare", "CemeteryAI", 2500, 450, hearseCount=7, corpseCapacity=105,
                  burialRate=45, graveCount=0, deathCareRadius=800)
CLINIC = asset("Clinic", "HealthCare", "HospitalAI", 1000, 250, ambulanceCount=8, patientCapacity=100,
               curingRate=20, healthCareRadius=700)
ASSETS = [LANDFILL, INCINERATOR, FIREHOUSE, FIRE_STATION, CEMETERY, CREMATORY, CLINIC]


def names(options):
    return [option["name"] for option in options]


class ServiceOptionTests(unittest.TestCase):
    def test_roles_tell_storing_from_processing(self):
        landfill, incinerator = (service_options.describe(item) for item in (LANDFILL, INCINERATOR))
        self.assertTrue(landfill["fillsUp"])
        self.assertFalse(incinerator["fillsUp"])  # its capacity is only a buffer before burning
        self.assertEqual({"radius": 2000.0, "vehicles": 27.0, "processing": 3000.0, "power": 750.0},
                         incinerator["roles"])
        self.assertTrue(service_options.describe(CEMETERY)["fillsUp"])  # it buries into graves

    def test_family_selects_by_ai_type_within_the_service(self):
        self.assertEqual(["Cemetery", "Crematory"], names(service_options.options_for(ASSETS, "deathcare", "coverage")))
        self.assertEqual(["Clinic"], names(service_options.options_for(ASSETS, "healthcare", "coverage")))

    def test_need_changes_the_choice(self):
        self.assertEqual("Landfill", service_options.options_for(ASSETS, "garbage", "coverage")[0]["name"])
        self.assertEqual("Incinerator", service_options.options_for(ASSETS, "garbage", "fillsUp")[0]["name"])
        self.assertEqual("Crematory", service_options.options_for(ASSETS, "deathcare", "fillsUp")[0]["name"])

    def test_deficit_counts_buildings_and_sites(self):
        ranked = service_options.options_for(ASSETS, "fire", "coverage", deficit=1100, site_cost=300)
        self.assertEqual([("FireStation", 1), ("Firehouse", 3)],
                         [(option["name"], option["buildingsNeeded"]) for option in ranked])
        self.assertEqual(-(3 * 350 + 2 * 300), ranked[1]["score"])

    def test_option_without_the_needed_role_ranks_last(self):
        ranked = service_options.options_for(ASSETS, "garbage", "fillsUp", deficit=3000)
        self.assertEqual((1, None), (ranked[0]["buildingsNeeded"], ranked[1]["buildingsNeeded"]))

    def test_budget_ranks_unaffordable_options_last(self):
        ranked = service_options.options_for(ASSETS, "garbage", "fillsUp", budget=1000)
        self.assertEqual(["Landfill", "Incinerator"], names(ranked))
        self.assertFalse(ranked[1]["affordable"])

    def test_a_school_family_takes_only_its_own_level(self):
        elementary = {**asset("Elementary School", "Education", "SchoolAI", 600, 120, studentCount=300,
                              educationRadius=500), "level": "Level1"}
        high = {**asset("High School", "Education", "SchoolAI", 2600, 520, studentCount=1000,
                        educationRadius=1000), "level": "Level2"}
        schools = [high, elementary]
        self.assertEqual(["Elementary School"], names(service_options.options_for(schools, "elementary", "capacity")))
        self.assertEqual(["High School"], names(service_options.options_for(schools, "highSchool", "capacity")))
        self.assertEqual([], names(service_options.options_for(schools, "university", "capacity")))
        self.assertEqual(2, len(service_options.options_for(schools, "education", "capacity")))

    def test_unknown_need_is_rejected(self):
        with self.assertRaises(ValueError):
            service_options.options_for(ASSETS, "garbage", "speed")


class CompareServiceOptionsToolTests(unittest.TestCase):
    def test_tool_reads_the_family_service_and_uses_cash_as_budget(self):
        calls = []

        def request(method, path, query=None, body=None):
            calls.append((path, query))
            if path == "/prefabs/buildings":
                return {"buildings": [LANDFILL, INCINERATOR]}
            if path == "/state/city-health":
                return {"cash": {"amount": 2000}}
            raise AssertionError(path)

        with mock.patch.object(mayor_tools, "_request", side_effect=request):
            result = mayor_tools.call_tool("compare_service_options", {"family": "garbage", "need": "fillsUp"})
        self.assertEqual(("/prefabs/buildings", {"service": "Garbage"}), calls[0])
        self.assertEqual(2000, result["budget"])
        self.assertEqual(["Landfill", "Incinerator"], names(result["options"]))  # 3000 is over budget


if __name__ == "__main__":
    unittest.main()
