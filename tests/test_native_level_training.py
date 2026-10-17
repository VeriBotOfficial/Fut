"""Training command response contract, receipts and the real ARM64 decoder.

The optional native tests execute the actual root-experience branch and the
six-field decoder. JSON library access is a host seam; Android/UI rendering
and the rest of CMD_USE_MYCLUB_ITEM are outside this test's coverage.
"""
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import unittest

import test_native_stats_settlement as fixtures
import club_store as store
import player_training

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'build/native-analysis-deps'))
try:
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
    from unicorn import arm64_const as reg
    HAVE_UNICORN = True
except ImportError:
    HAVE_UNICORN = False

LEVEL_FIELDS = ('level', 'level_limit', 'exp_total', 'exp_current_level',
                'exp_to_next_level', 'customize_point')
SOURCE_SHA256 = '2cea0c85344d7d7f295aea1c2353621f02db78dbccd888a0a21a0508ac330659'


class TrainingDecoder:
    """Run raw 0x7947980..79b4 and 0x7553b48 without replacing their logic."""
    OBJ = 0x20000000
    STACK = 0x30010000
    START, END = 0x7947980, 0x79479b4

    def __init__(self, source):
        self.uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        for va in (0x7947000, 0x7553000):
            self.uc.mem_map(va, 4096)
            self.uc.mem_write(va, source[va-0x4000:va-0x4000+4096])
        self.uc.mem_map(0xa00000, 0x300000)
        self.uc.mem_write(0xa00000, source[0xa00000:0xd00000])
        for va in (0x2fcc000, 0x2fcb000):
            self.uc.mem_map(va, 4096)
        self.uc.mem_map(self.OBJ, 0x10000)
        self.uc.mem_map(self.STACK-0x10000, 0x11000)
        # Constructor 0x79476d8 copies precisely these first two int32s, then
        # zeroes the remaining level-info fields; do not read adjacent data.
        self.defaults = struct.unpack_from('<2i', source, 0xc31008) + (0, 0, 0, 0)
        self.uc.hook_add(UC_HOOK_CODE, self.on_code)

    def r(self, number):
        return self.uc.reg_read(getattr(reg, 'UC_ARM64_REG_X' + str(number)))

    def w(self, number, value):
        self.uc.reg_write(getattr(reg, 'UC_ARM64_REG_X' + str(number)), value)

    def cstr(self, ptr):
        return bytes(self.uc.mem_read(ptr, 80)).split(b'\0', 1)[0].decode('ascii')

    def on_code(self, uc, address, size, _):
        if address in (0x2fcc828, 0x2fccaf0):
            obj = self.nodes.get(self.r(0))
            key = self.cstr(self.r(1))
            value = obj.get(key) if isinstance(obj, dict) else None
            self.lookups.append(key)
            ptr = self.next_node
            self.next_node += 0x100
            self.nodes[ptr] = value
            self.w(0, ptr)
        elif address == 0x2fcbaec:
            self.w(0, int(self.nodes.get(self.r(0)) is not None))
        elif address == 0x2fcbde8:
            value = self.nodes[self.r(0)]
            assert type(value) is int
            self.w(0, value & 0xffffffff)
        else:
            return
        uc.reg_write(reg.UC_ARM64_REG_PC, self.r(30))

    def decode(self, response):
        self.nodes = {self.OBJ+0xf0: response}
        self.next_node = 0x40000000
        self.lookups = []
        self.uc.mem_write(self.OBJ+0x960, struct.pack('<6i', *self.defaults))
        self.w(19, self.OBJ+0xf0)
        self.w(24, self.OBJ)
        self.uc.reg_write(reg.UC_ARM64_REG_SP, self.STACK)
        self.uc.emu_start(self.START, self.END, count=5000)
        assert self.uc.reg_read(reg.UC_ARM64_REG_PC) == self.END
        assert self.uc.reg_read(reg.UC_ARM64_REG_SP) == self.STACK
        values = struct.unpack('<6i', self.uc.mem_read(self.OBJ+0x960, 24))
        return dict(zip(LEVEL_FIELDS, values))


class LevelTrainingResponseTest(unittest.TestCase):
    setUp_fixture = fixtures.NativeStatsSettlementTest.setUp
    restart = fixtures.NativeStatsSettlementTest.restart

    def setUp(self):
        self.setUp_fixture()
        self.gid = {'id': 12, 'serial': 1}
        self.player = store.player_by_key(self.conn, 12, 1)
        self.player['level_info'] = dict(zip(LEVEL_FIELDS, (1, 10, 0, 0, 100, 0)))
        self.conn.execute('update gameplayers set json=? where id=12 and serial=1',
                          (json.dumps(self.player),))
        self.conn.commit()
        self.req = {'rqid': 'training-first', 'user_id': 7,
                    'gameplayer_id_list': [self.gid],
                    'item_list': [{'item': {'item_type': 'LEVEL_TRAINING', 'grade': 1, 'effect': 100},
                                   'present_num': 1}]}

    def train(self, req=None):
        return self.gate.handle('CMD_USE_MYCLUB_ITEM', copy.deepcopy(self.req if req is None else req))

    def owned(self):
        return store.player_by_key(self.conn, 12, 1)

    def test_training_immediate_experience_matches_persisted_level_and_points(self):
        other = store.player_by_key(self.conn, 12, 2)
        reply = self.train()
        self.assertEqual(reply['result'], 'NOERR')
        self.assertEqual(reply['experience'], self.owned()['level_info'])
        self.assertEqual(reply['experience']['level'], 2)
        self.assertEqual(reply['experience']['customize_point'], 2)
        self.assertEqual(store.player_by_key(self.conn, 12, 2), other)
        saved = self.owned()
        self.restart()
        self.assertEqual(self.owned(), saved)
        self.assertEqual(self.train()['experience'], reply['experience'])
        self.assertEqual(self.owned(), saved)

    def test_old_receipt_missing_experience_uses_its_snapshot_without_reapplying_xp(self):
        first = self.train()
        saved_first = copy.deepcopy(first['experience'])
        old = json.loads(self.conn.execute('select response_json from level_training_receipts').fetchone()[0])
        del old['experience']
        self.conn.execute('update level_training_receipts set response_json=?', (json.dumps(old),))
        self.conn.commit()
        second_req = copy.deepcopy(self.req)
        second_req['rqid'] = 'training-second'
        second = self.train(second_req)
        self.assertNotEqual(second['experience']['exp_total'], saved_first['exp_total'])
        saved_player = self.owned()
        self.restart()
        retry = self.train()
        self.assertEqual(retry['experience'], saved_first)
        self.assertEqual(self.owned(), saved_player)
        cached = json.loads(self.conn.execute('select response_json from level_training_receipts '
                                             'where rqid=?', ('training-first',)).fetchone()[0])
        self.assertEqual(cached['experience'], saved_first)
        self.assertEqual(self.conn.execute("select count(*) from events where kind='level_training'").fetchone()[0], 2)

    def test_conflicting_retry_does_not_train_or_rewrite_receipt(self):
        self.train()
        before = self.owned()
        old = self.conn.execute('select response_json from level_training_receipts').fetchone()[0]
        changed = copy.deepcopy(self.req)
        changed['item_list'][0]['present_num'] = 2
        self.assertEqual(self.train(changed)['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(self.owned(), before)
        self.assertEqual(self.conn.execute('select response_json from level_training_receipts').fetchone()[0], old)

    def test_receipt_repair_requires_exact_identity_and_valid_saved_level_info(self):
        response = self.train()
        for change in ('identity', 'level-zero', 'negative-points', 'missing-field'):
            with self.subTest(change=change):
                old = copy.deepcopy(response)
                del old['experience']
                row = old['gameplayer_exp_list'][0]
                if change == 'identity':
                    row['gameplayer_id']['serial'] = 2
                elif change == 'level-zero':
                    row['exp_info_postmatch']['level'] = 0
                elif change == 'negative-points':
                    row['exp_info_postmatch']['customize_point'] = -2
                else:
                    del row['exp_info_postmatch']['exp_total']
                before = copy.deepcopy(old)
                self.assertFalse(player_training.restore_use_item_experience(old, self.gid))
                self.assertEqual(old, before)

    @unittest.skipUnless(HAVE_UNICORN, 'optional native analysis dependencies unavailable')
    def test_shipped_arm64_decoder_explains_old_zero_level_and_accepts_actual_gateway_reply(self):
        path = ROOT / 'build/native-progression-audit/current-source-libUE4.so'
        if not path.is_file():
            self.skipTest('identified native library unavailable')
        source = path.read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(), SOURCE_SHA256)
        machine = TrainingDecoder(source)
        response = self.train()
        broken = copy.deepcopy(response)
        del broken['experience']
        old_decoded = machine.decode(broken)
        self.assertEqual(old_decoded, dict(zip(LEVEL_FIELDS, (0, 100, 0, 0, 0, 0))))
        self.assertEqual(machine.lookups, ['experience'])
        new_decoded = machine.decode(response)
        self.assertEqual(new_decoded, self.owned()['level_info'])
        self.assertEqual(new_decoded['level'], 2)
        self.assertEqual(new_decoded['customize_point'], 2)
        self.assertEqual(set(machine.lookups), {'experience', *LEVEL_FIELDS})


if __name__ == '__main__':
    unittest.main()
