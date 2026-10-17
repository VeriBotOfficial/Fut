"""Current resource-table provenance and complete league leaderboard coverage."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch
import zipfile
import zlib

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'gateway/work/tools'))
sys.path.insert(0, str(REPO / 'tools'))
import club_store as store
import extract_native_league_rosters as native
import season_rankings as rankings

CANONICAL = REPO / 'gateway/outputs/reference/canonical_rosters.json'
REPAIRED = {1249: 35, 5145: 23, 5776: 24, 5778: 23, 5779: 25, 5780: 25,
            5782: 24, 5783: 25, 5785: 22, 5786: 24, 5787: 23, 5788: 28,
            5793: 27, 5794: 28, 5795: 26, 5798: 27, 5800: 25, 5801: 24,
            5802: 23, 5803: 29, 5804: 26, 5833: 25, 5835: 23,
            5971: 23, 5972: 22, 6091: 26}


class CanonicalLeagueRosterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(CANONICAL.read_text(encoding='utf8'))

    def test_reviewed_data_counts_and_all_roster_positions_are_complete(self):
        self.assertEqual(self.data['validation'], dict(assignment_count=24050,
            team_count=834, unique_player_count=22900, player_table_count=46610,
            team_table_count=1015))
        # Windows Git may convert checkout line endings. Compare the canonical
        # UTF-8/LF serialization, while source binary hashes remain byte-exact.
        self.assertEqual(hashlib.sha256(CANONICAL.read_text(encoding='utf8').encode('utf8')).hexdigest(),
                         '5495d0562e290bafae0f329b6464ce9ecf3aaeeb95c76a4daa9c94afe3496f8a')
        used = set()
        for team, roster in self.data['rosters'].items():
            self.assertTrue(11 <= len(roster) <= 40, team)
            self.assertEqual(len(roster), len(set(roster)), team)
            self.assertTrue(any(self.data['positions'][str(pid)] != 0 for pid in roster), team)
            for pid in roster:
                self.assertIn(self.data['positions'][str(pid)], range(13))
            used.update(roster)
        self.assertEqual(len(used), 22900)

    def test_all_26_previously_missing_teams_have_native_rosters(self):
        seen = set()
        for team, count in REPAIRED.items():
            roster = self.data['rosters'][str(team)]
            self.assertEqual(len(roster), count, team)
            self.assertFalse(seen.intersection(roster), team)
            seen.update(roster)

    def test_every_current_league_team_is_covered(self):
        registry = REPO / 'build/gateway-fixture/outputs/reference/ziru-data/ml_competition_registry.json'
        if not registry.is_file():
            self.skipTest('Extracted native league registry unavailable')
        competitions = json.loads(registry.read_text(encoding='utf8'))['competitions']
        teams = {team for comp in competitions.values() for team in comp['team_ids']}
        self.assertEqual(len(teams), 441)
        self.assertEqual(teams - {int(team) for team in self.data['rosters']}, set())

    def test_all_834_rosters_simulate_only_team_players_and_conserve_scores(self):
        conn = sqlite3.connect(':memory:'); self.addCleanup(conn.close)
        conn.executescript(store.SCHEMA)
        teams = sorted(int(team) for team in self.data['rosters'])
        matches = [dict(home_team_id=team, away_team_id=teams[(index + 1) % len(teams)],
                        home_goals=2, away_goals=1) for index, team in enumerate(teams)]
        season = dict(season=1, competition_id=149, team_id=999999)
        with patch.object(rankings, 'CANONICAL_PATH', CANONICAL):
            conn.execute('BEGIN IMMEDIATE')
            rankings.record_fixture(conn, season, 1, matches); conn.commit()
            response = rankings.populate(conn, season, {})
        rows = json.loads(conn.execute('SELECT value FROM meta').fetchone()[0])
        self.assertEqual(rows['unavailable_teams'], [])
        scores, assists = Counter(), Counter()
        for field, counter in (('goal_ranking', scores), ('assist_ranking', assists)):
            for row in response[field]:
                self.assertEqual(row['is_com'], 'YES')
                self.assertIn(row['player_id'], self.data['rosters'][str(row['team_id'])])
                self.assertNotEqual(self.data['positions'][str(row['player_id'])], 0)
                counter[row['team_id']] += row['value']
        self.assertEqual(scores, Counter({team: 3 for team in teams}))
        self.assertTrue(all(assists[team] <= scores[team] for team in teams))

    def test_resource_zip_reproduces_reviewed_json_without_any_network(self):
        archive = Path('D:/ef/73efootball-resources.zip')
        if not archive.is_file():
            self.skipTest('Original user-provided resource ZIP unavailable')
        generated = native.extract(archive)
        self.assertEqual(native.serialized(generated), CANONICAL.read_text(encoding='utf8'))

    def test_wesys_rejects_changed_lengths_or_corrupt_encrypted_payload(self):
        archive = Path('D:/ef/73efootball-resources.zip')
        if not archive.is_file():
            self.skipTest('Original user-provided resource ZIP unavailable')
        with zipfile.ZipFile(archive) as source:
            tables = native.cpk_tables(source.read(native.CPK_MEMBER))
        raw = tables['PlayerAssignment.bin']
        changed = bytearray(raw); changed[8] ^= 1
        with self.assertRaisesRegex(ValueError, 'length mismatch'):
            native.unwrap_wesys(changed)
        changed = bytearray(raw); changed[16] ^= 0x80
        with self.assertRaises((ValueError, zlib.error)):
            native.unwrap_wesys(changed)


if __name__ == '__main__':
    unittest.main()
