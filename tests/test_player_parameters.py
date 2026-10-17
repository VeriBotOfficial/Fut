import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway/work/tools'))
import player_parameters as parameters


def policy():
    return {'schema': 1, 'columns': list(parameters.POLICY_COLUMNS),
            'cards': {'101': ['Epic', 34, 1, 2, 1, 14, 0]},
            'labels': {'101': ['Test Messi', 19]}}


class ParameterTests(unittest.TestCase):
    def test_repairs_only_identity_and_preserves_weak_grades(self):
        values = [0] * 147
        values[33], values[45] = 2, 1
        values[17], values[80], values[144], values[145] = 103, 1, 9, 6
        player = {'gameplayer_id': {'id': 101, 'serial': 5}, 'parameters': values,
                  'initial_booster_id_list': [4, 0, 0]}
        before = copy.deepcopy(player)
        result = parameters.owned_parameters(player, policy())
        self.assertEqual([result[i] for i in (43, 61, 62)], [1, 14, 0])
        self.assertEqual({i for i in range(147) if result[i] != values[i]}, {43, 61})
        self.assertEqual(player, before)

    def test_weak_foot_uses_zero_based_enum_without_shift(self):
        for accuracy in range(4):
            for usage in range(4):
                with self.subTest(accuracy=accuracy, usage=usage):
                    values = [0] * 147
                    values[33], values[45] = accuracy, usage
                    parameters.validate_weak_foot(values)
                    result = parameters.owned_parameters({'gameplayer_id': {'id': 101}, 'parameters': values}, policy())
                    self.assertEqual((result[33], result[45]), (accuracy, usage))
        for value in (-1, 4, True, None):
            values = [0] * 147
            values[45] = value
            with self.assertRaises(ValueError):
                parameters.validate_weak_foot(values)

    def test_unknown_layout_and_unknown_card_are_preserved(self):
        for count in (100, 148, 149):
            values = [1] * count
            self.assertEqual(parameters.owned_parameters({'gameplayer_id': {'id': 101}, 'parameters': values}, policy()), values)
        values = [0] * 147
        values[43], values[61] = 1, 21
        self.assertEqual(parameters.owned_parameters({'gameplayer_id': {'id': 999}, 'parameters': values}, policy()), values)

    def test_legacy_boosters_and_identity_are_converted_together(self):
        values = [0] * 145
        values[33], values[45], values[142], values[143] = 3, 0, 7, 8
        for old in (values, values + [0, 0, 0, 0]):
            player = {'gameplayer_id': {'id': 101}, 'parameters': old, 'initial_booster_id_list': [7, 8, 0]}
            result = parameters.owned_parameters(player, policy())
            self.assertEqual(len(result), 147)
            self.assertEqual(result[142:], [0, 0, 7, 8, 0])
            self.assertEqual([result[i] for i in (33, 43, 45, 61, 62)], [3, 1, 0, 14, 0])

    def test_bundled_policy_is_valid_and_excludes_unclassified_cards(self):
        document = parameters.validate_policy(parameters.card_policy())
        self.assertEqual(len(document['cards']), 7092)
        self.assertNotIn('137957', document['cards'])
        self.assertNotIn('37638', document['cards'])
        self.assertEqual(sum(row[0] != 'Standard' for row in document['cards'].values()), 6328)
        self.assertTrue(all(row[1] == 1 for row in document['cards'].values() if row[0] == 'Trending'))
        sample = parameters.card_details(89136409091415)
        self.assertEqual((sample['age'], sample['foot'], sample['attack_style']), (19, 1, 14))
        self.assertTrue(sample['name'])
        self.assertIsNone(parameters.card_details(137957))

    def test_invalid_policy_enum_and_trending_progression_are_rejected(self):
        for index, value in ((1, 0), (2, 2), (3, 4), (4, True), (5, 36)):
            document = policy()
            document['cards']['101'][index] = value
            with self.assertRaises(ValueError):
                parameters.validate_policy(document)
        document = policy()
        document['cards']['101'][0] = 'Trending'
        with self.assertRaises(ValueError):
            parameters.validate_policy(document)


if __name__ == '__main__':
    unittest.main()
