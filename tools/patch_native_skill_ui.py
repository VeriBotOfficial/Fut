"""Bounded native skill-selector catalogue patch for the identified test engine.

The original selector intersects six hard-coded ordinary-skill lists with the
server's skillcard_id_list. This replacement keeps those categories and their
order, adds supported ordinary/ST skills, and preserves that same intersection.
The original selection, descriptions, confirmation and request producer stay
native. No gameplay parameter mapping or skill effect implementation is changed.

Evidence: build/native-skill-ui/{7baf758,35de4a4,35dea6c,35dedd0,
35df70c,7452fc0}.c. VAs below are original ELF VAs, without Ghidra's 0x100000
image-base slide. The ARM64 frame/save layout matches the replaced function.
"""
from __future__ import annotations

import hashlib

SOURCE_SHA256 = '2cea0c85344d7d7f295aea1c2353621f02db78dbccd888a0a21a0508ac330659'
FUNCTION_VA = 0x7BAF758
FUNCTION_END_VA = 0x7BB3488
FILE_OFFSET = FUNCTION_VA - 0x4000

# Exact original categories, obtained by executing the source ARM64 function.
ORIGINAL_CATEGORIES = (
    (28, 32, 33, 34, 35, 37, 38, 39, 44),  # passing
    (0, 1, 2, 3, 4, 5, 6, 7, 10),         # dribbling
    (34, 56, 57, 58, 59, 60, 61, 65),     # defending
    (17, 19, 20, 21, 22, 23, 27, 29, 49), # shooting
    (32, 33, 34, 44, 45, 46, 48, 50, 72), # goalkeeping
    (15, 47, 49, 55, 60, 69, 71, 72),     # other
)
ADDED_CATEGORIES = (
    (36, 40, 41, 42),
    (8, 11, 12, 13, 73),
    (62, 64, 66, 67),
    (16, 18, 24, 30),
    (53, 54),
    (),
)
CATEGORIES = tuple(original + added for original, added in zip(ORIGINAL_CATEGORIES, ADDED_CATEGORIES))

# Each row is count + up to 15 uint32 IDs, padded to 64 bytes. The original
# std::vector uses three pointers (begin/end/capacity); its own allocator and
# deallocator remain paired. w2 bit 0 retains the native "unfiltered" option.
ASSEMBLY = '''
stp x29, x30, [sp, #-0x40]!
stp x24, x23, [sp, #0x10]
stp x22, x21, [sp, #0x20]
stp x20, x19, [sp, #0x30]
mov x29, sp
mov x19, x0
mov w20, w2
mov w22, w1
ldr x0, [x19]
cbz x0, cleared
bl #0x2f376d4
cleared:
stp xzr, xzr, [x19]
str xzr, [x19, #16]
cmp w22, #5
b.hi done
adr x21, catalogue
add x21, x21, x22, lsl #6
ldr w22, [x21], #4
lsl w0, w22, #2
mov w1, #0
bl #0x2f37644
stp x0, x0, [x19]
add x8, x0, x22, lsl #2
str x8, [x19, #16]
tbnz w20, #0, unfiltered
mov w0, #1
bl #0x7a87db8
cbz x0, unfiltered
ldr x0, [x0, #0x28]
bl #0x7a729d8
cbz x0, unfiltered
add x0, x0, #0x2a0
ldp x23, x24, [x0]
mov w20, #0
b ready
unfiltered:
mov w20, #1
ready:
ldr x11, [x19]
next:
cbz w22, finish
ldr w8, [x21], #4
tbnz w20, #0, include
mov x9, x23
scan:
cmp x9, x24
b.hs reject
ldr w10, [x9], #4
cmp w10, w8
b.ne scan
include:
str w8, [x11], #4
reject:
sub w22, w22, #1
b next
finish:
str x11, [x19, #8]
done:
mov x0, x19
b #0x7bb33b8
catalogue:
''' + '\n'.join('.word ' + ', '.join(map(str, (len(row), *row, *([0] * (15-len(row))))))
                 for row in CATEGORIES)

# Generated once from ASSEMBLY using Keystone; builds need no assembler.
PATCH_BYTES = bytes.fromhex('fd7bbca9f85f01a9f65702a9f44f03a9fd030091f30300aaf403022af603012a600240f9400000b4d51fce967f7e00a97f0a00f9df16007188040054b5040010b51a168bb64640b8c0761e5301008052a71fce96600200a90808168b680a00f974010037200080527e61fb97000100b4001440f9830cfb97a00000b400800a91176040a91400805202000014340080526b0240f996010034a84640b8f4000037e90317aa3f0118eba20000542a4540b85f01086b81ffff54684500b8d6060051f5ffff176b0600f9e00313aae50e00140d0000001c000000200000002100000022000000230000002500000026000000270000002c0000002400000028000000290000002a00000000000000000000000e00000000000000010000000200000003000000040000000500000006000000070000000a000000080000000b0000000c0000000d00000049000000000000000c0000002200000038000000390000003a0000003b0000003c0000003d000000410000003e0000004000000042000000430000000000000000000000000000000d0000001100000013000000140000001500000016000000170000001b0000001d000000310000001000000012000000180000001e00000000000000000000000b0000002000000021000000220000002c0000002d0000002e000000300000003200000048000000350000003600000000000000000000000000000000000000080000000f0000002f00000031000000370000003c00000045000000470000004800000000000000000000000000000000000000000000000000000000000000')
ORIGINAL_BLOCK_SHA256 = '63fd6dcf6f02c63ec260bf4ccb23d2816d85ffc8514d2b0f159c3a2e43376950'


def patches(source: bytes) -> list[dict]:
    """Return a checked, equal-length patch against the original source only."""
    end = FILE_OFFSET + len(PATCH_BYTES)
    before = source[FILE_OFFSET:end]
    if before == PATCH_BYTES:
        raise ValueError('native skill selector patch is already applied')
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise ValueError('native skill selector source SHA-256 mismatch')
    if not PATCH_BYTES or FUNCTION_VA + len(PATCH_BYTES) > FUNCTION_END_VA:
        raise ValueError('native skill selector patch exceeds the identified function')
    if hashlib.sha256(before).hexdigest() != ORIGINAL_BLOCK_SHA256:
        raise ValueError('native skill selector original instruction bytes mismatch')
    return [dict(name='native_skill_selector_all_supported', offset=FILE_OFFSET,
                 before=before, after=PATCH_BYTES)]
