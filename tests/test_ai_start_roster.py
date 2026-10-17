"""The AI match snapshot must be the actual response roster, not optional input."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'gateway/work/tools'))

import ai_division
import club_store as store
import gate_logic
import match_protocol as protocol


class AiStartRosterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'club.sqlite3'
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.conn.executescript(store.SCHEMA)
        self.conn.execute('CREATE TABLE owned_identity_aliases('
                          'id INTEGER,old_serial INTEGER,new_serial INTEGER)')
        store.set_meta(self.conn, 'owned_identity_version', '1')
        store.set_meta(self.conn, 'base_team_id', '101')
        self.refs = [{'gameplayer_id': {'id': 9000+i, 'serial': i+1}}
                     for i in range(23)]
        for row in self.refs:
            gid = row['gameplayer_id']
            player = dict(row, parameters=[0]*147, additional_skill_id=[],
                          initial_booster_id_list=[0, 0, 0])
            self.conn.execute('INSERT INTO gameplayers(id,serial,json) VALUES(?,?,?)',
                              (gid['id'], gid['serial'], json.dumps(player)))
        self.squad = {'squad_id': 1, 'base_team': [{'base_team_id': 101}],
                      'squad_data': {'player_list': copy.deepcopy(self.refs)}}
        store.save_squad(self.conn, self.squad)
        store.set_meta(self.conn, 'ai_division_active_match', json.dumps({
            'game_id': 100, 'round': 1, 'division': 10, 'started': False, 'side': 'HOME'}))
        self.conn.commit()
        self.gate = gate_logic.GateLogic({}, conn=self.conn)

    def request(self, **extra):
        return dict(game_id=100, match_category='LEAGUE_COM', squad_id=1,
                    rqid=10, user_id=7, **extra)

    def start(self, **extra):
        return self.gate._start_game({'msgid': 'CMD_START_GAME', 'result': 'NOERR',
                                     'squad_data_home': {}, 'commentary_info': {}},
                                    self.request(**extra))

    def active(self):
        return protocol.active_match(self.conn, protocol.AI)

    def test_omitted_request_roster_snapshots_all_resolved_starters_and_bench(self):
        response = self.start()
        sent = response['squad_data_home']['player_list']
        self.assertEqual([row['gameplayer_id'] for row in sent],
                         [row['gameplayer_id'] for row in self.refs])
        self.assertEqual(self.active()['eligible_player_instances'],
                         [[row['gameplayer_id']['id'], row['gameplayer_id']['serial']] for row in sent])
        self.assertEqual(len(self.active()['eligible_player_instances']), 23)

    def test_resolved_entries_take_precedence_over_raw_request_in_start_api(self):
        resolved = list(reversed(self.refs))
        self.assertTrue(ai_division.start(self.conn, self.request(player_list_home=self.refs), resolved))
        self.assertEqual(self.active()['eligible_player_instances'],
                         [[row['gameplayer_id']['id'], row['gameplayer_id']['serial']] for row in resolved])

    def test_restart_retry_preserves_original_snapshot_and_rejects_reordered_roster(self):
        self.start()
        original = self.active()
        self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.gate = gate_logic.GateLogic({}, conn=self.conn)
        self.assertEqual(self.start()['result'], 'NOERR')
        self.assertEqual(self.active(), original)
        rejected = self.start(player_list_home=list(reversed(self.refs)))
        self.assertEqual(rejected['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(self.active(), original)

    def test_invalid_roster_does_not_mark_match_started(self):
        for refs in ([self.refs[0], self.refs[0]],
                     [{'gameplayer_id': {'id': 999999, 'serial': 999999}}]):
            with self.subTest(refs=refs):
                with self.assertRaises(ValueError):
                    self.start(player_list_home=refs)
                self.assertFalse(self.active()['started'])
                self.assertNotIn('eligible_player_instances', self.active())

    def test_empty_saved_roster_does_not_mark_match_started(self):
        self.squad['squad_data']['player_list'] = []
        store.save_squad(self.conn, self.squad)
        self.conn.commit()
        with self.assertRaises(protocol.MatchError):
            self.start()
        self.assertFalse(self.active()['started'])


if __name__ == '__main__':
    unittest.main()
