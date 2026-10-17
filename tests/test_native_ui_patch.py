"""Reject mismatched/overlapping binary proposals before creating a payload."""
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import native_ui_patch


class NativePatchCompositionTest(unittest.TestCase):
    def setUp(self):
        self.source = bytes(range(128))
        self.sha = hashlib.sha256(self.source).hexdigest()

    def patch(self, offset=16, before=None, after=b'UI!!', name='native UI test'):
        return dict(offset=offset, before=self.source[offset:offset + 4] if before is None else before,
                    after=after, name=name)

    def apply(self, patches, source=None):
        return native_ui_patch.apply_plan(self.source if source is None else source, patches,
                                         expected_sha256=self.sha)

    def test_composition_preserves_all_unmodified_bytes_and_records_exact_ranges(self):
        result, report = self.apply([self.patch(60), self.patch(16, after=b'FPS!')])
        self.assertEqual(len(result), len(self.source))
        self.assertEqual(result[:16], self.source[:16])
        self.assertEqual(result[16:20], b'FPS!')
        self.assertEqual(result[20:60], self.source[20:60])
        self.assertEqual(result[60:64], b'UI!!')
        self.assertEqual(result[64:], self.source[64:])
        self.assertEqual(report['sourceSha256'], self.sha)
        self.assertEqual(report['patchedSha256'], hashlib.sha256(result).hexdigest())
        self.assertEqual([p['offset'] for p in report['ranges']], [16, 60])
        self.assertTrue(report['unmodifiedRangesVerified'])

    def test_wrong_source_even_outside_a_patch_is_rejected(self):
        changed = self.source[:-1] + b'X'
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.apply([self.patch()], changed)

    def test_changed_original_instruction_or_overlap_is_rejected(self):
        for proposals in ([self.patch(before=b'nope')], [self.patch(16), self.patch(18)],
                          [self.patch(16), self.patch(16)]):
            with self.subTest(proposals=proposals), self.assertRaises(ValueError):
                self.apply(proposals)

    def test_invalid_or_empty_range_cannot_silently_disable_verification(self):
        cases = [[], [self.patch(-1)], [self.patch(True)], [self.patch(126)],
                 [self.patch(after=b'longer')], [self.patch(before=b'', after=b'')],
                 [self.patch(after=self.source[16:20])], [self.patch(name='')],
                 [dict(name='bad', offset=16, before='10111213', after=b'UI!!')]]
        for proposals in cases:
            with self.subTest(proposals=proposals), self.assertRaises(ValueError):
                self.apply(proposals)


if __name__ == '__main__':
    unittest.main()
