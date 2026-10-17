"""Bounded native FPS menu patch; native render cap enum 5 is 120 FPS.

The existing four menu IDs retain their order and meaning. The new fifth entry
maps UI index 4 to saved/native enum 5, and the getter reverses this mapping.
Only the added private text ID returns "120"; all existing localization paths
and the native render-cap table are preserved. This does not fix simulation
timesteps or guarantee a device can sustain 120 FPS.

Offsets are raw ELF VAs minus 0x4000. Builder replacement remains entirely
inside 0x748a094..0x748a574. No new ELF segment or borrowed skill-patch space.
Generated ARM64 is checked by the native execution tests. Static evidence and
scope limits are in docs/native-fps-menu.md.
"""
from __future__ import annotations

import hashlib

SOURCE_SHA256 = '2cea0c85344d7d7f295aea1c2353621f02db78dbccd888a0a21a0508ac330659'
ORIGINAL_IDS = (0x4710169, 0x471016a, 0x4710254, 0x4710253)
NEW_ID = 2147418400
BUILDER_VA = 122200212
BUILDER_END_VA = 0x748A574

# Reviewable source; production builds use the checked bytes below.
ASSEMBLIES = {
    0x748a094: """
stp x29, x30, [sp, #-0x40]!
str x23, [sp, #0x10]
stp x22, x21, [sp, #0x20]
stp x20, x19, [sp, #0x30]
mov x29, sp

mov x19, x1
ldp x21, x22, [x19]
sub x22, x22, x21
add x0, x22, #20
mov w1, wzr
bl #0x2f37644
mov x23, x0
mov x9, x21
add x10, x21, x22
copy:
cmp x9, x10
b.eq append
ldr w11, [x9], #4
str w11, [x0], #4
b copy
append:
adr x9, labels
ldp w10, w11, [x9]
stp w10, w11, [x0], #8
ldp w10, w11, [x9, #8]
stp w10, w11, [x0], #8
ldr w10, [x9, #16]
str w10, [x0], #4
stp x23, x0, [x19]
str x0, [x19, #16]
mov x0, x21
bl #0x2f376d4
b #0x748a448
labels:
.word 0x4710169, 0x471016a, 0x4710254, 0x4710253, 0x7fff0120
""",
    0x748a194: """
stp x29, x30, [sp, #-0x40]!
str x23, [sp, #0x10]
stp x22, x21, [sp, #0x20]
stp x20, x19, [sp, #0x30]
mov x29, sp

ldrb w0, [x9, x8]
cmp w0, #5
cset w8, eq
sub w0, w0, w8

ldp x20, x19, [sp, #0x30]
ldp x22, x21, [sp, #0x20]
ldr x23, [sp, #0x10]
ldp x29, x30, [sp], #0x40

b #0x748a58c
""",
    0x748a204: """
stp x29, x30, [sp, #-0x40]!
str x23, [sp, #0x10]
stp x22, x21, [sp, #0x20]
stp x20, x19, [sp, #0x30]
mov x29, sp

cmp w1, #4
cinc w1, w1, eq

ldp x20, x19, [sp, #0x30]
ldp x22, x21, [sp, #0x20]
ldr x23, [sp, #0x10]
ldp x29, x30, [sp], #0x40

mov w19, w1
b #0x748a5a4
""",
    0x748a274: """
mov w16, #0x120
movk w16, #0x7fff, lsl #16
cmp w0, w16
b.eq custom
mov x1, x8
stp xzr, xzr, [x8]
b #0x3bbf130
custom:

stp x29, x30, [sp, #-0x40]!
str x23, [sp, #0x10]
stp x22, x21, [sp, #0x20]
stp x20, x19, [sp, #0x30]
mov x29, sp

mov x19, x8
stp xzr, xzr, [x19]
mov x0, x19
mov w1, #4
bl #0x286ff60
ldr x9, [x19]
mov x10, #0x31
movk x10, #0x32, lsl #16
movk x10, #0x30, lsl #32
str x10, [x9]
mov w10, #4
str w10, [x19, #8]

ldp x20, x19, [sp, #0x30]
ldp x22, x21, [sp, #0x20]
ldr x23, [sp, #0x10]
ldp x29, x30, [sp], #0x40

ret
""",
    0x748a334: """
mov w16, #0x120
movk w16, #0x7fff, lsl #16
cmp w1, w16
b.ne original
adrp x0, #122200064
add x0, x0, #868
ret
original:
stp x29, x30, [sp, #-0x40]!
b #0x81f36b4
""",
}

# (name, raw VA, original block digest, replacement bytes)
BLOCKS = (
    ('native_fps_fifth_option_and_helpers', 0x748a094, '52d459648ca74b5c47b50c0555ac34b4b1e0f6cf56e14d278e8d694b5939f2ac',
     bytes.fromhex('fd7bbca9f70b00f9f65702a9f44f03a9fd030091f30301aa755a40a9d60215cbc0520091e1031f2a62b5ea96f70300aae90315aaaa02168b3f010aeb800000542b4540b80b4400b8fcffff17890100102a2d40290a2c81282a2d41290a2c81282a1140b90a4400b8770200a9600a00f9e00315aa73b5ea96cf000014690171046a01710454027104530271042001ff7f1f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d5fd7bbca9f70b00f9f65702a9f44f03a9fd030091206968381f140071e8179f1a0000084bf44f43a9f65742a9f70b40f9fd7bc4a8f10000141f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d5fd7bbca9f70b00f9f65702a9f44f03a9fd0300913f1000712114811af44f43a9f65742a9f70b40f9fd7bc4a8f303012adc0000141f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d510248052f0ffaf721f00106b80000054e10308aa1f7d00a9a9d31c17fd7bbca9f70b00f9f65702a9f44f03a9fd030091f30308aa7f7e00a9e00313aa810080522b97cf96690240f92a0680d24a06a0f20a06c0f22a0100f98a0080526a0a00b9f44f43a9f65742a9f70b40f9fd7bc4a8c0035fd61f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d510248052f0ffaf723f00106b810000540000009000900d91c0035fd6fd7bbca9d8a435141f2003d51f2003d51f2003d5313230001f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d51f2003d5')),
    ('native_fps_saved_enum_to_menu_index', 0x748a588, '62d849302ca8702f657041962f9c97a13f30fc0c96bc71233aa842aba6db7a8b',
     bytes.fromhex('03ffff17')),
    ('native_fps_menu_index_to_saved_enum', 0x748a5a0, '591c05dd8cc241b487ce74ac10f64f3a1044d198ab81fccf345e7333537fe35e',
     bytes.fromhex('19ffff17')),
    ('native_fps_label_fstring', 0x3a555b0, 'f8f09f512f3a1f6d5fe8f4267e45e3901f251fe2bf7477efc0c3161b22ba51fc',
     bytes.fromhex('31d3e814')),
    ('native_fps_label_string_table', 0x81f36b0, 'ffe18035cc86cd3f6d0d931d003d1ec8a7aff5c15ba0ea0695ebf17e3c85341a',
     bytes.fromhex('215bca17')),
)


def patches(source: bytes) -> list[dict]:
    """Return composable equal-length ranges for the exact shipped library."""
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise ValueError("native FPS patch source SHA-256 mismatch")
    result = []
    for name, va, digest, after in BLOCKS:
        offset = va - 0x4000
        before = source[offset:offset + len(after)]
        if hashlib.sha256(before).hexdigest() != digest:
            raise ValueError("native FPS original instruction bytes mismatch: " + name)
        result.append(dict(name=name, offset=offset, before=before, after=after))
    return result
