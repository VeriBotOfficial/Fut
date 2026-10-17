"""Execute the real selector ARM64 before/after the bounded catalogue patch.

Native allocator and game-context calls are emulated; selection/filtering,
vector writes, control flow and AAPCS64 register/frame preservation execute in
Unicorn. This does not claim Android rendering/device validation.
"""
from pathlib import Path
import hashlib
import io
import struct
import sys
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'gateway/work/tools'),
               str(ROOT / 'build/native-analysis-deps')]
import patch_native_skill_ui as patch
import player_skills
try:
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_INVALID
    from unicorn.arm64_const import *
except ImportError:
    Uc = None

START=0x7baf758;END=0x7bb3488
OUT=0x200000000;STATE=OUT+0x10000;CTX=OUT+0x30000;WHITE=OUT+0x40000
STACK=0x300000000;STOP=0x400000000
HELPERS={0x2f37644:'alloc',0x2f376d4:'free',0x8c1bc60:'copy',0x8c1bc70:'copy',0x7a87db8:'state',0x7a729d8:'context',0x8c1ec40:'throw'}
def execute(raw,category,whitelist=range(75),unfiltered=0,missing_state=False,initial=()):
 u=Uc(UC_ARCH_ARM64,UC_MODE_ARM);u.mem_map(0xa00000,0x400000);u.mem_write(0xa00000,raw[0xa00000:0xe00000]);base=START&~4095
 u.mem_map(base,((END-base+4095)//4096)*4096);u.mem_write(base,raw[base-0x4000:END-0x4000])
 for p in set(x&~4095 for x in HELPERS):u.mem_map(p,4096)
 u.mem_map(OUT,0x400000);u.mem_map(STACK,0x10000);u.mem_map(STOP,4096)
 u.mem_write(STATE+0x28,struct.pack('<Q',CTX));w=list(whitelist)
 if w:u.mem_write(WHITE,struct.pack('<%dI'%len(w),*w))
 u.mem_write(CTX+0x2a0,struct.pack('<2Q',WHITE,WHITE+4*len(w)))
 pos=[OUT+0x100000];allocs=[];frees=[]
 if initial:
  q=pos[0];pos[0]+=4096;u.mem_write(q,struct.pack('<%dI'%len(initial),*initial));u.mem_write(OUT,struct.pack('<3Q',q,q+4*len(initial),q+4*len(initial)))
 def hook(uc,address,size,user):
  if address not in HELPERS:return
  kind=HELPERS[address];a=uc.reg_read(UC_ARM64_REG_X0);b=uc.reg_read(UC_ARM64_REG_X1);c=uc.reg_read(UC_ARM64_REG_X2)
  if kind=='alloc':
   if not 0<a<0x10000:raise AssertionError(('allocation',a))
   result=pos[0];pos[0]+=(a+255)&~255;allocs.append((result,a))
  elif kind=='free':frees.append(a);result=0
  elif kind=='copy':uc.mem_write(a,bytes(uc.mem_read(b,c)));result=a
  elif kind=='state':result=0 if missing_state else STATE
  elif kind=='context':result=CTX
  else:raise AssertionError(('throw',hex(uc.reg_read(UC_ARM64_REG_LR))))
  uc.reg_write(UC_ARM64_REG_X0,result);uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_LR))
 u.hook_add(UC_HOOK_CODE,hook)
 u.hook_add(UC_HOOK_MEM_INVALID,lambda uc,access,addr,size,val,data: print("BAD",hex(uc.reg_read(UC_ARM64_REG_PC)),hex(addr),size) or False)
 u.reg_write(UC_ARM64_REG_SP,STACK+0x8000);u.reg_write(UC_ARM64_REG_LR,STOP)
 for r in range(UC_ARM64_REG_X19,UC_ARM64_REG_X28+1):u.reg_write(r,0x67890123+r)
 u.reg_write(UC_ARM64_REG_X0,OUT);u.reg_write(UC_ARM64_REG_X1,category);u.reg_write(UC_ARM64_REG_X2,unfiltered)
 u.emu_start(START,STOP,count=500000)
 assert u.reg_read(UC_ARM64_REG_PC)==STOP
 for r in range(UC_ARM64_REG_X19,UC_ARM64_REG_X28+1):assert u.reg_read(r)==0x67890123+r,('clobber',r)
 assert u.reg_read(UC_ARM64_REG_SP)==STACK+0x8000
 begin,end,cap=struct.unpack('<3Q',u.mem_read(OUT,24));assert begin<=end<=cap
 values=list(struct.unpack('<%dI'%((end-begin)//4),u.mem_read(begin,end-begin))) if end>begin else []
 return values,{'allocated':allocs,'freed':frees,'vector':(begin,end,cap)}

class NativeSkillSelectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if Uc is None:
            raise unittest.SkipTest('Unicorn is required to execute the native selector')
        path = ROOT / 'build/match-stats-investigation/libUE4.so'
        if not path.is_file():
            raise unittest.SkipTest('identified source libUE4.so is required')
        cls.source = path.read_bytes()
        if hashlib.sha256(cls.source).hexdigest() != patch.SOURCE_SHA256:
            raise AssertionError('analysis source does not match the identified APK member')
        cls.parts = patch.patches(cls.source)
        cls.patched = bytearray(cls.source)
        for entry in cls.parts:
            a = entry['offset']; b = a + len(entry['before'])
            cls.patched[a:b] = entry['after']
        cls.patched = bytes(cls.patched)

    def test_exact_source_and_only_declared_bytes_change(self):
        entry, = self.parts
        off = entry['offset']; n = len(entry['after'])
        self.assertEqual(entry['before'], self.source[off:off+n])
        self.assertEqual(len(entry['before']), n)
        self.assertEqual(len(self.source), len(self.patched))
        self.assertEqual(self.source[:off], self.patched[:off])
        self.assertEqual(self.source[off+n:], self.patched[off+n:])
        self.assertLessEqual(patch.FUNCTION_VA+n, patch.FUNCTION_END_VA)
        self.assertEqual(hashlib.sha256(self.patched).hexdigest(),
                         'df76b8934521a5a5cdcafaf9150a31aee6b79b049ee62223680dba69fbe36cf2')

    def test_reject_changed_source_and_reapplication(self):
        with self.assertRaisesRegex(ValueError, 'already applied'):
            patch.patches(self.patched)
        changed = bytearray(self.source); changed[16] ^= 1
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            patch.patches(bytes(changed))
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            patch.patches(b'unsupported engine')

    def test_stored_machine_code_matches_assembly(self):
        try:
            from keystone import Ks, KS_ARCH_ARM64, KS_MODE_LITTLE_ENDIAN
        except ImportError:
            self.skipTest('Keystone is required to reproduce the bounded patch')
        actual = bytes(Ks(KS_ARCH_ARM64, KS_MODE_LITTLE_ENDIAN).asm(
            patch.ASSEMBLY, patch.FUNCTION_VA)[0])
        self.assertEqual(actual, patch.PATCH_BYTES)

    def test_original_categories_from_real_engine_and_all_63_supported(self):
        union = set()
        for category in range(6):
            with self.subTest(category=category):
                old, _ = execute(self.source, category, unfiltered=1)
                new, _ = execute(self.patched, category, unfiltered=1)
                self.assertEqual(old, list(patch.ORIGINAL_CATEGORIES[category]))
                self.assertEqual(new, list(patch.CATEGORIES[category]))
                self.assertEqual(new[:len(old)], old)
                self.assertEqual(len(new), len(set(new)))
                union.update(new)
        self.assertEqual(union, set(player_skills.SELECTABLE_SKILL_IDS))
        self.assertEqual(len(union), 63)

    def test_whitelist_intersection_and_order(self):
        for category in range(6):
            for white in ([], [0, 8, 11, 12, 13, 73], [42, 42, 70, 71, 72, 73],
                          list(range(74, -1, -1)), [999, 1000]):
                with self.subTest(category=category, white=white):
                    found, _ = execute(self.patched, category, whitelist=white)
                    self.assertEqual(found, [x for x in patch.CATEGORIES[category] if x in white])

    def test_unfiltered_native_flag_uses_only_bit_zero(self):
        for flag in (0, 1, 2, 3):
            actual, _ = execute(self.patched, 1, whitelist=[], unfiltered=flag)
            self.assertEqual(actual, list(patch.CATEGORIES[1]) if flag & 1 else [])

    def test_missing_game_state_preserves_native_unfiltered_fallback(self):
        for category in range(6):
            self.assertEqual(execute(self.source, category, whitelist=[], missing_state=True)[0],
                             list(patch.ORIGINAL_CATEGORIES[category]))
            self.assertEqual(execute(self.patched, category, whitelist=[], missing_state=True)[0],
                             list(patch.CATEGORIES[category]))

    def test_invalid_categories_return_empty_and_release_previous_buffer(self):
        for category in (6, 99, 0xffffffff):
            actual, state = execute(self.patched, category, initial=(88, 99))
            self.assertEqual(actual, [])
            self.assertEqual(len(state['freed']), 1)
            self.assertEqual(state['allocated'], [])

    def test_existing_vector_is_replaced_and_freed_once(self):
        for category in range(6):
            actual, state = execute(self.patched, category, initial=(88, 99))
            self.assertEqual(actual, list(patch.CATEGORIES[category]))
            self.assertEqual(len(state['freed']), 1)
            self.assertEqual(len(state['allocated']), 1)

    def test_real_native_delete_shifts_following_slots_and_appends_empty(self):
        # DELETE response 0x78c29d0 invokes this helper; reproducing its actual
        # behavior prevents UI/saved-slot divergence after the next training.
        start, end = 0x7a88288, 0x7a88330
        for original in ([11, 12, 13, 16, 18], [74, 74, 18, 74, 73]):
            for index in range(5):
                u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
                page = start & ~4095
                u.mem_map(page, 4096)
                u.mem_write(page, self.source[page-0x4000:page-0x4000+4096])
                u.mem_map(OUT, 4096); u.mem_map(STOP, 4096)
                u.mem_write(OUT+0x64, struct.pack('<5I', *original))
                u.reg_write(UC_ARM64_REG_X0, OUT)
                u.reg_write(UC_ARM64_REG_X1, index)
                u.reg_write(UC_ARM64_REG_LR, STOP)
                u.emu_start(start, STOP, count=100)
                self.assertEqual(u.reg_read(UC_ARM64_REG_PC), STOP)
                actual = list(struct.unpack('<5I', u.mem_read(OUT+0x64, 20)))
                self.assertEqual(actual, original[:index]+original[index+1:]+[74])

    def test_original_prologue_epilogue_and_cfi_are_reused(self):
        from capstone import Cs, CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN
        from elftools.elf.elffile import ELFFile
        from elftools.dwarf.callframe import CallFrameInfo
        self.assertEqual(patch.PATCH_BYTES[:20], self.source[patch.FILE_OFFSET:patch.FILE_OFFSET+20])
        code = list(Cs(CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN).disasm(
            patch.PATCH_BYTES[:208], patch.FUNCTION_VA))
        self.assertEqual((code[-1].mnemonic, code[-1].op_str), ('b', '#0x7bb33b8'))
        elf = ELFFile(io.BytesIO(self.source)); section = elf.get_section_by_name('.eh_frame')
        parser = CallFrameInfo(io.BytesIO(section.data()), section['sh_size'], section['sh_addr'],
                               elf.get_dwarf_info().structs, for_eh_frame=True)
        fde = parser._parse_entry_at(0x2602168-section['sh_addr'])
        rows = fde.get_decoded().table
        for insn in code:
            if insn.mnemonic == 'bl':
                row = max((x for x in rows if x['pc'] <= insn.address+4), key=lambda x: x['pc'])
                self.assertEqual((row['cfa'].reg, row['cfa'].offset), (29, 64))
                self.assertEqual(row[30].arg, -56)
        self.assertEqual(self.patched[0x7bb33b8-0x4000:0x7bb33cc-0x4000],
                         self.source[0x7bb33b8-0x4000:0x7bb33cc-0x4000])

if __name__ == '__main__':
    unittest.main()
