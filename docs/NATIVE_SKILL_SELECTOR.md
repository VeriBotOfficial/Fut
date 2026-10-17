# Native skill selection and saved slots

## Identified engine and patch

- Input APK: `release/v1.1.1/efootxi-1.1.1.apk`, SHA-256 `0a612a8153843c8bfe5fb515eac5b983a6ec377990eaf8635c03034b1f5ae255`.
- Its `lib/arm64-v8a/libUE4.so` is byte-identical to the analyzed `build/match-stats-investigation/libUE4.so`; SHA-256 `2cea0c85344d7d7f295aea1c2353621f02db78dbccd888a0a21a0508ac330659`.
- `tools/patch_native_skill_ui.py:patches(source)` returns one equal-length edit: file offset `0x7bab758`, original ELF VA `0x7baf758`, length 592 bytes. The original function ends at VA `0x7bb3488`.
- With this patch alone, SHA-256 is `df76b8934521a5a5cdcafaf9150a31aee6b79b049ee62223680dba69fbe36cf2`. Composing the independently bounded FPS patch changes this digest.
- The original source SHA and replaced-block digest are required. Reapplication, a different engine, or an out-of-function edit is rejected. The ELF layout and all bytes outside the declared edit stay unchanged.

The helper previously hard-coded six ordinary-skill categories before intersecting each with the player's `skillcard_id_list`. Returning 63 IDs from the gateway therefore did not expose ST skills in the native selector. The patch preserves each existing category's order, appends only the missing supported choices, and retains the original whitelist intersection. Its union is exactly the current 63 selectable IDs (45 ordinary and 18 ST). Existing native row labels, descriptions, category selection, confirmation, and request production remain in use; no additional window is introduced. The unchanged native label helper receives the same ID later sent in `ability_id`.

The first 20 prologue bytes, 0x40-byte stack frame and saved-register offsets match the source. The replacement branches to the untouched original epilogue at `0x7bb33b8`; its FDE at `0x2602168` therefore remains valid both at native calls and during return. A complete direct-branch scan of the executable segment found seven external references, all to the function entry and none to the overwritten interior. The Android APS2 relative-relocation table contains no pointer to its overwritten region.

## Native entry and protocol evidence

All addresses below are original ELF VAs. Saved Ghidra pseudocode uses an extra `0x100000` image base; subtract it when following a displayed callee address.

| Evidence | Observed behavior |
| --- | --- |
| `0x35dc42c` -> `0x7452260` | Original skill-training screen enables the selected-skill item when utility type 9, effect 0 has more than 99 items. Existing inventory supplies `SKILL_TRAINING_SELECT`, grade 0, effect 0, count 99999. No per-skill ticket catalogue is needed. |
| `0x35de4a4`, `0x35dea6c`, `0x35dedd0`, `0x35df70c` | Native list, row selection, descriptions and confirmation all call `0x7baf758` with the current category and the same filtered order. |
| `0x7452fc0` | Confirmation builds utility `{type=9, chosen_id, 0, count=100}`. |
| `0x7943be8`, case 9; serializer `0x79455ac` | Wire request has `ability_id=chosen_id`, `training_index=context+0x288`, `item={item_type:SKILL_TRAINING_SELECT, grade:0, effect:chosen_id}`, `present_num=100`. Skill IDs are not parameter-array offsets. |
| `0x35dd1c8` -> `0x74521f8` | The clicked native list index is passed unchanged both to `GetItemAt` and to the context's training slot. |
| `0x794447c` | On success the native client replaces exactly `context.training_index`, checking it is less than 5. This is zero-based, 0 through 4. |
| `0x765b97c`, table `0xc9df1c` | Wire skill IDs 0 through 73 map identically; IDs outside that range map to internal 74. Native skill-slot reads at `0x7453284` and response handling treat 74 as an empty slot. It is not offered as a learned effect. |

The former gateway interpreted slot 0 as append and slots 1..5 as one-based replacement. Five normal clicks at slots 0,1,2,3,4 consequently saved only four skills: the second click replaced the first, while the native client immediately displayed the second slot. Reopening the game revealed the saved mismatch. The gateway now uses the native slot unchanged; selecting a later empty slot inserts empty markers without moving other skills. Existing saved skills remain in their original order; previously overwritten choices are not invented or reconstructed.

Native skill deletion is also connected to SQLite. Its serializer at `0x78c2bac` sends `gameplayer_id:{id,serial}` and `delete_skill_index`; the success callback at `0x78c29d0` invokes `0x7a88288`. That helper shifts the following slots left and writes empty marker 74 to the last slot. The server deliberately reproduces this compaction (different from assigning a new skill to an empty position), keeping the other skills and their relative order. A Unicorn check executes the actual helper for all five deletion indices, including sparse lists. Deleting every skill changes `is_skill_added` to `NO` in both detail and entry snapshots. Deleting a middle slot, restarting, and training again therefore use the same positions in the native UI and save.

Skill receipts are scoped to `session_id` when present, because request counters may restart after login. Within a session, retries remain idempotent and return the current saved five-slot state rather than an older snapshot.

## Validation and limits

`tests/test_native_skill_selector.py` executes original and patched ARM64 in Unicorn with the engine's allocator/context calls stubbed. It checks all categories, all 63 IDs, empty/sparse/reversed/duplicate whitelists, the native unfiltered flag, invalid categories, old-vector release, preserved registers/stack, exact unchanged byte ranges, source rejection, assembly reproducibility and FDE reuse.

`tests/test_skills_persistence.py`, `tests/test_skills_gateway.py`, and the independent `tests/test_native_skill_contract.py` exercise SQLite reopening, exact player copies, all five slots, slot replacement, native request fields, same-session retries, new-session counter reuse, response/detail/entry snapshots and match projections. `tests/test_native_skill_delete.py` additionally covers native deletion, refill, retry/session boundaries, invalid identities and transaction rollback. These are host-side and emulated checks. They do not establish Android rendering behavior or every skill effect on a physical phone.
