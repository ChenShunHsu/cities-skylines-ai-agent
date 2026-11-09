import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop import city as city_module  # noqa: E402
from loop import growth, land  # noqa: E402

SIZE = 1920.0


def tile(index, x, z, price, can=True, owned=False):
    return {"index": index, "x": x, "z": z, "price": price, "canUnlock": can, "unlocked": owned}


# the live offer at 1,541 people: the town on tile (3, 1), four neighbours for 2,500-5,300
OFFER = [tile(3, 3, 0, 470000), tile(7, 2, 1, 300000), tile(8, 3, 1, 0, can=False, owned=True),
         tile(9, 4, 1, 250000), tile(13, 3, 2, 530000)]
TOWN = (2700.0, -1900.0)  # on tile (3, 1), by its east edge: tile 9 (4, 1) is nearest


def flat(share, count=100):
    return [{"slope": 0.01 if i < share * count else 0.3, "water": {"present": False}} for i in range(count)]


class ChooseTileTests(unittest.TestCase):
    def test_the_offered_tile_nearest_the_town_with_ground_to_build_on(self):
        shares = {3: 0.8, 7: 0.8, 9: 0.8, 13: 0.8}
        self.assertEqual(9, land.choose_tile(OFFER, shares, TOWN, 52000, SIZE)["index"])

    def test_a_steep_or_flooded_tile_is_passed_over(self):
        self.assertEqual(0.25, land.flat_share(flat(0.25)))
        wet = [{"slope": 0.0, "water": {"present": True}}] * 3 + [{"slope": 0.0, "water": {"present": False}}]
        self.assertEqual(0.25, land.flat_share(wet))
        shares = {3: 0.8, 7: 0.8, 9: 0.1, 13: 0.8}
        self.assertNotEqual(9, land.choose_tile(OFFER, shares, TOWN, 52000, SIZE)["index"])

    def test_a_tile_that_would_leave_less_than_the_reserve_is_not_bought(self):
        shares = {3: 0.8, 7: 0.8, 9: 0.8, 13: 0.8}
        self.assertIsNone(land.choose_tile(OFFER, shares, TOWN, land.LAND_RESERVE + 1000, SIZE))
        self.assertEqual(9, land.choose_tile(OFFER, shares, TOWN, land.LAND_RESERVE + 2500, SIZE)["index"])


class BuyTests(unittest.TestCase):
    def request(self, count=1, cash=5211241):
        def request(method, path, query=None, body=None):
            if path == "/state/map-areas":
                return {"areaCount": count, "maxAreaCount": 9, "areas": OFFER}
            if path == "/state/city-health":
                return {"cash": {"amount": cash}}
            if path == "/state/map-analysis":
                return {"cells": flat(0.8)}
            raise AssertionError(path)
        return request

    def buy(self, state, **kwargs):
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            with mock.patch.object(land.mt, "_request", side_effect=self.request(**kwargs)), \
                    mock.patch.object(land.protocol, "run", return_value={"ok": True, "regressionOk": True}) as run:
                result = land.buy(city, "a", state, TOWN)
        return result, run

    def test_one_tile_is_bought_then_none_for_a_few_wakes(self):
        state = {"wake": 10}
        result, run = self.buy(state)
        self.assertTrue(result["ok"])
        self.assertEqual((9, 2500.0), (result["tile"], result["price"]))
        self.assertEqual({"tileIndex": 9, "reserveDisplay": land.LAND_RESERVE}, run.call_args.args[2])
        state["wake"] = 10 + land.BUY_EVERY - 1
        result, run = self.buy(state)
        self.assertEqual("recentlyBought", result["stage"])
        run.assert_not_called()
        state["wake"] = 10 + land.BUY_EVERY
        self.assertTrue(self.buy(state)[0]["ok"])

    def test_nothing_is_bought_at_the_tile_limit(self):
        result, run = self.buy({"wake": 1}, count=9)
        self.assertEqual("tileLimit", result["stage"])
        run.assert_not_called()


class GrowLandTests(unittest.TestCase):
    def grow(self, expansion, bought):
        categories = {use: {"demand": 80, "developableVacantCells": 0, "maximumAdditionalBlocksThisWake": 2}
                      for use in growth.USES}
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            with mock.patch.object(growth, "ensure_outside", return_value=None), \
                    mock.patch.object(growth.mt, "call_tool", return_value={"growthPacing": {"categories": categories}}), \
                    mock.patch.object(growth, "zone_blocks", return_value=[]), \
                    mock.patch.object(growth, "expand", return_value=expansion), \
                    mock.patch.object(growth, "_town", return_value=TOWN), \
                    mock.patch.object(growth.land, "buy", return_value=bought) as buy:
                report = growth.grow(city, "a")
        return report, buy

    def test_a_use_with_no_land_it_can_reach_buys_a_tile_instead_of_stopping(self):
        stuck = {"use": "workplace", "ok": False, "stage": "access", "refused": [{"nodeId": 30939}]}
        report, buy = self.grow(stuck, {"ok": True, "tile": 9})
        self.assertEqual(1, buy.call_count)
        self.assertEqual(("boughtLand", None), (report["expansion"]["stage"], report["expansion"]["ok"]))
        self.assertEqual("access", report["expansion"]["stuck"]["stage"])
        # play stops on an expansion that failed (ok False) or needs a crossing, not on land bought
        self.assertIsNot(False, report["expansion"]["ok"])
        self.assertNotEqual("needsCrossing", report["expansion"]["stage"])

    def test_with_no_tile_to_buy_the_stuck_use_still_goes_to_the_planner(self):
        stuck = {"use": "workplace", "ok": False, "stage": "access", "refused": [{"nodeId": 30939}]}
        report, _ = self.grow(stuck, {"ok": None, "stage": "noTileToBuy"})
        self.assertEqual(("access", False), (report["expansion"]["stage"], report["expansion"]["ok"]))
        self.assertEqual("noTileToBuy", report["expansion"]["land"]["stage"])

    def test_land_out_of_reach_with_no_tile_to_buy_is_reported_once_then_waited_for(self):
        stuck = {"use": "workplace", "ok": False, "stage": "access", "refused": [{"nodeId": 30939}]}
        categories = {use: {"demand": 80, "developableVacantCells": 0, "maximumAdditionalBlocksThisWake": 2}
                      for use in growth.USES}
        stages = []
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            with mock.patch.object(growth, "ensure_outside", return_value=None), \
                    mock.patch.object(growth.mt, "call_tool", return_value={"growthPacing": {"categories": categories}}), \
                    mock.patch.object(growth, "zone_blocks", return_value=[]), \
                    mock.patch.object(growth, "expand", return_value=stuck), \
                    mock.patch.object(growth, "_town", return_value=TOWN), \
                    mock.patch.object(growth.land, "buy", return_value={"ok": None, "stage": "noTileToBuy"}):
                for _ in range(growth.LAND_REPORT_EVERY + 1):
                    expansion = growth.grow(city, "a")["expansion"]
                    stages.append((expansion["stage"], expansion["ok"]))
        self.assertEqual([("access", False)] + [("landLimited", None)] * (growth.LAND_REPORT_EVERY - 1)
                         + [("access", False)], stages)

    def test_a_growing_district_buys_nothing(self):
        report, buy = self.grow({"use": "residential", "ok": True, "collectorNodes": 4}, {"ok": True})
        buy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
