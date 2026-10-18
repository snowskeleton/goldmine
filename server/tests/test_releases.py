import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goldmine_server import addon  # noqa: E402

# The same files and hash are pinned in desktop/tests/test_client.py: client and server must agree.
HASH_TEST_FILES = {"Goldmine.toc": "## Version: 1.2\n", "Goldmine.lua": "print(1)\n", "sub/x.lua": "x\n",
                   "Data.lua": "ignored", ".hidden": "ignored"}
HASH_TEST_VECTOR = "634544549ee09d88b49a4ec8ae84ba8ae41458e2fad3d23e65efb4885e710b01"


class AddonPackaging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "Goldmine"
        for name, text in HASH_TEST_FILES.items():
            (self.src / name).parent.mkdir(parents=True, exist_ok=True)
            (self.src / name).write_text(text)

    def tearDown(self):
        self.tmp.cleanup()

    def test_hash_matches_client_vector(self):
        self.assertEqual(addon.content_hash(self.src), HASH_TEST_VECTOR)
        self.assertEqual(addon.toc_version(self.src), "1.2")

    def test_package_is_deterministic_and_skips_generated(self):
        a, b = addon.package(self.src), addon.package(self.src)
        self.assertEqual(a, b)
        self.assertEqual(sorted(zipfile.ZipFile(io.BytesIO(a)).namelist()),
                         ["Goldmine/Goldmine.lua", "Goldmine/Goldmine.toc", "Goldmine/sub/x.lua"])


if __name__ == "__main__":
    unittest.main()
