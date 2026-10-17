"""Execute the shipped and patched ARM64 routines, with allocator/config seams.

Optional analysis dependencies live in build/native-analysis-deps; ordinary APK
builds do not need these packages. No Android runtime or device is simulated.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import sys
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'tools'), str(ROOT/'build/native-analysis-deps')]
import patch_native_fps_ui as patch

try:
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
    from unicorn import arm64_const as reg
    HAVE_UNICORN = True
except ImportError:
    HAVE_UNICORN = False


class Machine:
    STOP = 0x10000000
    HEAP = 0x20000000
    STACK = 0x30010000

    def __init__(self, source, patched):
        self.uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        self.next = self.HEAP + 0x10000
        self.frees = []
        self.allocs = []
        self.legacy_text = []
        self.legacy_lookup = []
        pages = {0x748A000, 0x3A55000, 0x81F3000, 0x81F4000,
                 0x2F37000, 0x2F4B000, 0x286F000, 0x2870000,
                 0x8246000, 0x42CC000, 0x3BBF000, 0x8108000}
        for va in pages:
            self.uc.mem_map(va, 0x1000)
            self.uc.mem_write(va, source[va-0x4000:va-0x4000+0x1000])
        if patched:
            for _, va, _, after in patch.BLOCKS:
                self.uc.mem_write(va, after)
        self.uc.mem_map(self.STOP, 0x1000)
        self.uc.mem_map(self.HEAP, 0x100000)
        self.uc.mem_map(self.STACK-0x10000, 0x11000)
        self.uc.mem_map(0x9BAE000, 0x1000)
        self.obj = self.HEAP + 0x100
        self.config = self.HEAP + 0x1000
        self.write(self.obj+0x48, '<Q', self.config)
        self.uc.hook_add(UC_HOOK_CODE, self.on_code)

    def read(self, address, fmt):
        return struct.unpack(fmt, self.uc.mem_read(address, struct.calcsize(fmt)))

    def write(self, address, fmt, *values):
        self.uc.mem_write(address, struct.pack(fmt, *values))

    def r(self, number):
        return self.uc.reg_read(getattr(reg, 'UC_ARM64_REG_X'+str(number)))

    def w(self, number, value):
        self.uc.reg_write(getattr(reg, 'UC_ARM64_REG_X'+str(number)), value)

    def ret(self):
        self.uc.reg_write(reg.UC_ARM64_REG_PC, self.r(30))

    def alloc(self, size):
        ptr = self.next
        self.next += (size + 63) & ~63
        self.allocs.append((ptr, size))
        return ptr

    def on_code(self, uc, address, size, _):
        if address == self.STOP:
            uc.emu_stop()
        elif address == 0x2F37644:
            self.w(0, self.alloc(self.r(0)))
            self.ret()
        elif address == 0x2F376D4:
            self.frees.append(self.r(0))
            self.ret()
        elif address == 0x82468D4:
            self.w(0, self.obj)
            self.ret()
        elif address == 0x42CCBBC:
            # Platform allocation-size rounding; returning the exact request is valid.
            self.ret()
        elif address == 0x42CCA20:
            self.w(0, self.alloc(self.r(1)))
            self.ret()
        elif address == 0x3BBF130:
            self.legacy_text.append((self.r(0), self.r(1),
                                     bytes(uc.mem_read(self.r(1), 16))))
            self.ret()
        elif address == 0x81F36B4:
            # Verify all non-private IDs reach the exact original continuation.
            self.legacy_lookup.append((self.r(0), self.r(1),
                                       uc.reg_read(reg.UC_ARM64_REG_SP)))
            uc.emu_stop()

    def call(self, address, x0=0, x1=0, x8=0, continuation=False):
        self.w(0, x0)
        self.w(1, x1)
        self.w(8, x8)
        saved = {i: 0x12340000+i for i in range(19, 30)}
        for i, value in saved.items():
            self.w(i, value)
        self.w(30, self.STOP)
        self.uc.reg_write(reg.UC_ARM64_REG_SP, self.STACK)
        self.uc.emu_start(address, self.STOP+4, count=100000)
        if not continuation:
            assert self.uc.reg_read(reg.UC_ARM64_REG_PC) == self.STOP
            assert self.uc.reg_read(reg.UC_ARM64_REG_SP) == self.STACK
            for i, value in saved.items():
                assert self.r(i) == value, (i, self.r(i), value)
        return self.r(0)


@unittest.skipUnless(HAVE_UNICORN, 'install analysis-only Unicorn to run native execution tests')
class NativeFpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        apk = ROOT.parents[1]/'release/v1.1.1/efootxi-1.1.1.apk'
        if not apk.exists():
            raise unittest.SkipTest('identified source APK unavailable')
        with zipfile.ZipFile(apk) as z:
            cls.source = z.read('lib/arm64-v8a/libUE4.so')
        assert hashlib.sha256(cls.source).hexdigest() == patch.SOURCE_SHA256

    def test_exact_source_and_disjoint_equal_length_ranges(self):
        rows = patch.patches(self.source)
        for row in rows:
            self.assertEqual(len(row['before']), len(row['after']))
            self.assertEqual(row['before'], self.source[row['offset']:row['offset']+len(row['before'])])
        for a, b in zip(sorted(rows, key=lambda r:r['offset']), sorted(rows, key=lambda r:r['offset'])[1:]):
            self.assertLessEqual(a['offset']+len(a['after']), b['offset'])
        damaged = bytearray(self.source)
        damaged[0] ^= 1
        with self.assertRaises(ValueError):
            patch.patches(bytes(damaged))
        self.assertLessEqual(patch.BUILDER_VA+len(patch.BLOCKS[0][3]), patch.BUILDER_END_VA)

    def test_original_four_menu_options_preserved_and_fifth_appended(self):
        for prior in ([], [900], list(range(17))):
            for spare in (0, 16):
                results = []
                for modified in (False, True):
                    m = Machine(self.source, modified)
                    obj = m.HEAP+0x4000
                    data = m.alloc(4*(len(prior)+spare)) if prior or spare else 0
                    if prior:
                        m.write(data, '<'+'I'*len(prior), *prior)
                    m.write(obj, '<QQQ', data, data+4*len(prior), data+4*(len(prior)+spare))
                    m.call(patch.BUILDER_VA, 0xDEAD, obj)
                    start, end, cap = m.read(obj, '<QQQ')
                    self.assertGreaterEqual(cap, end)
                    results.append(list(m.read(start, '<'+'I'*((end-start)//4))))
                self.assertEqual(results[0], prior+list(patch.ORIGINAL_IDS))
                self.assertEqual(results[1], results[0]+[patch.NEW_ID])

    def test_setter_preserves_existing_options_and_stores_real_120_enum(self):
        for selected in [*range(14), 14, 255, 0xFFFFFFFF]:
            outputs = []
            for modified in (False, True):
                m = Machine(self.source, modified)
                m.call(0x748A594, 0xDEAD, selected)
                outputs.append((m.read(m.config+0x1955, '<B')[0], m.read(0x9BAEC18, '<I')[0]))
            if selected == 4:
                self.assertEqual(outputs[1], (5, 5))
            else:
                self.assertEqual(outputs[1], outputs[0])
        table = struct.unpack_from('<14i', self.source, 0xC7E530)
        self.assertEqual(table[5], 120)
        self.assertEqual(table[0], 60)

    def test_getter_maps_saved_120_back_to_fifth_menu_item(self):
        m = Machine(self.source, True)
        for enum in range(256):
            m.write(m.config+0x1955, '<B', enum)
            actual = m.call(0x748A574)
            self.assertEqual(actual, 4 if enum == 5 else enum)

    def test_existing_config_restore_applies_enum_without_clamping(self):
        m = Machine(self.source, True)
        for enum in (0, 1, 2, 3, 5):
            m.write(m.config+0x1955, '<B', enum)
            m.w(20, m.config+0x1955)
            m.uc.reg_write(reg.UC_ARM64_REG_SP, m.STACK)
            # Actual restore instructions: ldrb w0,[x20]; mov w1,#1; bl setter.
            m.uc.emu_start(0x810895C, 0x8108968, count=100)
            self.assertEqual(m.read(0x9BAEC18, '<I')[0], 1 if enum in (2, 3) else enum)

    def test_reviewable_assembly_matches_every_embedded_helper(self):
        try:
            from keystone import Ks, KS_ARCH_ARM64, KS_MODE_LITTLE_ENDIAN
        except ImportError:
            self.skipTest('Keystone assembler not installed')
        asm = Ks(KS_ARCH_ARM64, KS_MODE_LITTLE_ENDIAN)
        block = patch.BLOCKS[0][3]
        for address, assembly in patch.ASSEMBLIES.items():
            code = bytes(asm.asm(assembly, address)[0])
            start = address-patch.BUILDER_VA
            self.assertEqual(code, block[start:start+len(code)])

    def test_new_label_is_owned_native_fstring_and_legacy_path_unchanged(self):
        m = Machine(self.source, True)
        out = m.HEAP+0x4000
        m.call(0x3A555B0, patch.NEW_ID, x8=out)
        pointer, count, capacity = m.read(out, '<QII')
        self.assertEqual(count, 4)
        self.assertGreaterEqual(capacity, count)
        self.assertEqual(bytes(m.uc.mem_read(pointer, 8)), '120\0'.encode('utf-16-le'))
        self.assertEqual(m.legacy_text, [])
        for language_id in [*patch.ORIGINAL_IDS, 0, 0xFFFFFFFF]:
            m.write(out, '<QQ', 0xDEAD, 0xBEEF)
            m.call(0x3A555B0, language_id, x8=out)
            self.assertEqual(m.legacy_text[-1], (language_id, out, bytes(16)))

    def test_private_string_table_label_and_original_prologue(self):
        m = Machine(self.source, True)
        ptr = m.call(0x81F36B0, 0xDEAD, patch.NEW_ID)
        self.assertEqual(bytes(m.uc.mem_read(ptr, 4)), b'120\0')
        for language_id in [*patch.ORIGINAL_IDS, 0, 0xFFFFFFFF]:
            m.call(0x81F36B0, 0xDEAD, language_id, continuation=True)
            self.assertEqual(m.legacy_lookup[-1], (0xDEAD, language_id, m.STACK-0x40))
            self.assertEqual(m.read(m.STACK-0x40, '<QQ'), (0x12340000+29, m.STOP))


if __name__ == '__main__':
    unittest.main()
