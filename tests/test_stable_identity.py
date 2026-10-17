"""Formal 1.2 upgrades retain the real released package and private namespaces."""
import hashlib
from pathlib import Path
import struct
import sys
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import android_manifest as axml
import stable_identity as stable
import test_identity as isolated

BASELINE = ROOT.parents[1] / 'release/v1.1.1/efootxi-1.1.1.apk'
MANIFEST_SHA = 'b203b5a9faaa57d95d7b620b12cde4e6173a1d61d6df951abca5c9051bfabcb7'
RESOURCES_SHA = '33af59af91649f6e22965125b88d1b930a66ec76c8e535af9714a3dea32f67df'


def nodes(data):
    chunks = axml._chunks(data)
    strings = axml._strings(next(raw for kind, _, raw in chunks if kind == 1))
    return [(name, {(a['namespace'], a['name']): (a['type'], a['value']) for a in attrs})
            for kind, _, raw in chunks if kind == 0x102
            for name, attrs in [axml._node(raw, strings)]]


def change_attribute(data, element, name, value):
    """Make a controlled bad/legacy input without changing other manifest data."""
    chunks = axml._chunks(data)
    pool_i = next(i for i, (kind, _, _) in enumerate(chunks) if kind == 1)
    strings = axml._strings(chunks[pool_i][2])
    added, edits = [], {}
    for i, (kind, _, raw) in enumerate(chunks):
        if kind != 0x102:
            continue
        tag, attrs = axml._node(raw, strings)
        if tag != element:
            continue
        attribute = next(a for a in attrs if a['name'] == name)
        out = bytearray(raw)
        if type(value) is int:
            struct.pack_into('<I', out, attribute['offset'] + 16, value)
        else:
            if value in strings:
                index = strings.index(value)
            else:
                added.append(value)
                index = len(strings)
            struct.pack_into('<I', out, attribute['offset'] + 8, index)
            struct.pack_into('<I', out, attribute['offset'] + 16, index)
        edits[i] = bytes(out)
    result = bytearray(data[:8])
    for i, (_, _, raw) in enumerate(chunks):
        result.extend(axml._append_strings(raw, added) if i == pool_i else edits.get(i, raw))
    struct.pack_into('<I', result, 4, len(result))
    return bytes(result)


@unittest.skipUnless(BASELINE.is_file(), 'The identified released 1.1.1 APK is required')
class StableIdentityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with zipfile.ZipFile(BASELINE) as archive:
            cls.original = archive.read('AndroidManifest.xml')
            cls.resources = archive.read('resources.arsc')
        assert hashlib.sha256(cls.original).hexdigest() == MANIFEST_SHA
        assert hashlib.sha256(cls.resources).hexdigest() == RESOURCES_SHA
        cls.updated = stable.manifest(cls.original)

    def test_real_upgrade_retains_every_manifest_contract_except_version_and_label(self):
        before, after = nodes(self.original), nodes(self.updated)
        self.assertEqual(len(before), len(after))
        for (old_name, old), (new_name, new) in zip(before, after):
            self.assertEqual(old_name, new_name)
            for key in list(old):
                if (old_name == 'manifest' and key == (axml.ANDROID_NS, 'versionName') or
                        old_name == 'application' and key == (axml.ANDROID_NS, 'label')):
                    old.pop(key)
                    new.pop(key)
            self.assertEqual(old, new)
        root = next(attrs for name, attrs in nodes(self.updated) if name == 'manifest')
        self.assertEqual(root[(None, 'package')], (3, 'com.efootball.revival.local'))
        self.assertEqual(root[(axml.ANDROID_NS, 'versionCode')], (16, 311010101))
        self.assertEqual(root[(axml.ANDROID_NS, 'versionName')], (3, '1.2'))
        app = next(attrs for name, attrs in nodes(self.updated) if name == 'application')
        self.assertEqual(app[(axml.ANDROID_NS, 'label')], (3, 'efootxi'))

    def test_private_provider_and_permission_namespaces_remain_the_released_identity(self):
        before = [(name, attrs) for name, attrs in nodes(self.original)
                  if name in ('permission', 'uses-permission', 'provider')]
        after = [(name, attrs) for name, attrs in nodes(self.updated)
                 if name in ('permission', 'uses-permission', 'provider')]
        self.assertEqual(before, after)
        authorities = {attrs[(axml.ANDROID_NS, 'authorities')][1]: attrs
                       for name, attrs in after if name == 'provider'}
        provider = authorities['com.efootball.revival.local.efootxi.update']
        self.assertEqual(provider[(axml.ANDROID_NS, 'name')][1], axml.PROVIDER)
        self.assertEqual(provider[(axml.ANDROID_NS, 'exported')], (18, 0))
        self.assertEqual(provider[(axml.ANDROID_NS, 'grantUriPermissions')], (18, 0xffffffff))
        self.assertIn('com.efootball.revival.local.documents', authorities)
        self.assertTrue(all(value.startswith(stable.PACKAGE + '.') for value in authorities))
        permissions = {attrs[(axml.ANDROID_NS, 'name')][1]
                       for name, attrs in after if name in ('permission', 'uses-permission')}
        self.assertIn(stable.PACKAGE + '.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION', permissions)
        # Android uses the unchanged package for its existing private files/data
        # directory. No test namespace or renamed provider routes are introduced.
        self.assertFalse(any('.revival.test' in value for value in set(authorities) | permissions))

    def test_resource_ids_and_package_bytes_are_not_rebuilt_or_renamed(self):
        self.assertIs(stable.resource_table(self.resources), self.resources)
        self.assertEqual(hashlib.sha256(stable.resource_table(self.resources)).hexdigest(), RESOURCES_SHA)
        with self.assertRaisesRegex(ValueError, 'formal resource package'):
            stable.resource_table(isolated.resource_table(self.resources))

    def test_patching_the_same_formal_version_is_idempotent(self):
        self.assertEqual(stable.manifest(self.updated), self.updated)

    def test_isolated_test_package_cannot_be_used_as_a_formal_upgrade_source(self):
        with self.assertRaisesRegex(ValueError, 'application identity'):
            stable.manifest(isolated.manifest(self.original))

    def test_native_game_version_code_cannot_be_changed_by_the_upgrade(self):
        altered = change_attribute(self.original, 'manifest', 'versionCode', 311010102)
        with self.assertRaisesRegex(ValueError, 'game version code'):
            stable.manifest(altered)

    def test_legacy_label_changes_only_the_application_label_and_version(self):
        legacy = change_attribute(self.original, 'application', 'label', '73efootball')
        updated = stable.manifest(legacy)
        self.assertEqual(nodes(updated), nodes(self.updated))


if __name__ == '__main__':
    unittest.main()

