"""Contract checks only; live unlock and UI availability must be inspected in-game."""
import unittest
from unittest import mock
import mayor_tools


class TerrainEditingTests(unittest.TestCase):
    def test_inspection_returns_native_lock_evidence_without_a_mutation(self):
        evidence = {'ok': True, 'nativeUnlocked': False, 'requiredMilestone': 'fixture',
                    'bridgeTerrainMutationImplemented': False, 'soilBufferNativeUnits': 150}
        with mock.patch.object(mayor_tools, '_request', return_value=evidence) as request:
            self.assertEqual(evidence, mayor_tools.call_tool('inspect_terrain_editing', {}))
        request.assert_called_once_with('GET', '/state/terrain-editing')

    def test_native_state_does_not_write_terrain_money_or_unlocks(self):
        source = (mayor_tools.ROOT / 'src/TerrainEditingState.cs').read_text(encoding='utf-8')
        self.assertIn('unlocks.Unlocked(UnlockManager.Feature.Landscaping)', source)
        self.assertIn('button.isEnabled && button.isVisible && button.isInteractive', source)
        for forbidden in ('FetchResource(', 'AddResource(', 'RawHeights', 'ApplyBrush(', 'SetTool<'):
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main()
