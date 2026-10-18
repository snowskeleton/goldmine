import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import addon  # noqa: E402
import goldmine  # noqa: E402


def cli(*args) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = goldmine.main(["--local", *args])
    return code, out.getvalue()


class AddonInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wow = Path(self.tmp.name) / "World of Warcraft"
        for flavor in ("_retail_", "_classic_"):
            (self.wow / flavor).mkdir(parents=True)
        self.args = ["--wow-dir", str(self.wow)]

    def tearDown(self):
        self.tmp.cleanup()

    def dest(self, flavor="_retail_") -> Path:
        return addon.target_for(self.wow / flavor)

    def test_package_is_deterministic_and_skips_data(self):
        a, b = addon.package(addon.BUNDLED), addon.package(addon.BUNDLED)
        self.assertEqual(a, b)
        names = zipfile.ZipFile(io.BytesIO(a)).namelist()
        self.assertIn("Goldmine/Goldmine.toc", names)
        self.assertNotIn("Goldmine/Data.lua", names)

    def test_install_needs_a_flavor_when_ambiguous(self):
        code, _ = cli("addon", "install", *self.args)
        self.assertEqual(code, 1)
        self.assertFalse(self.dest().exists())

    def test_install_update_keeps_data_lua(self):
        code, out = cli("addon", "install", "--flavor", "_retail_", *self.args)
        self.assertEqual(code, 0, out)
        self.assertIn("_retail_: installed", out)
        self.assertEqual(addon.content_hash(self.dest()), addon.content_hash(addon.BUNDLED))
        self.assertFalse(self.dest("_classic_").exists())

        (self.dest() / "Data.lua").write_text("Goldmine_Data = {}")
        (self.dest() / "Goldmine.lua").write_text("-- old version")
        st = json.loads(cli("addon", "status", "--json", *self.args)[1])
        retail = next(c for c in st["copies"] if c["flavor"] == "_retail_")
        self.assertEqual((retail["installed"], retail["current"]), (True, False))

        code, out = cli("addon", "install", "--auto", *self.args)  # updates installed copies only
        self.assertEqual(code, 0, out)
        self.assertIn("_retail_: updated", out)
        self.assertEqual((self.dest() / "Data.lua").read_text(), "Goldmine_Data = {}")
        self.assertEqual(addon.content_hash(self.dest()), addon.content_hash(addon.BUNDLED))
        self.assertFalse(self.dest("_classic_").exists())
        self.assertEqual([p.name for p in self.dest().parent.iterdir()], ["Goldmine"])  # no staging left

        code, out = cli("addon", "install", *self.args)
        self.assertIn("_retail_: current", out)

    def test_linked_dev_copy_left_alone(self):
        self.dest().parent.mkdir(parents=True)
        os.symlink(addon.BUNDLED, self.dest())
        code, out = cli("addon", "install", "--flavor", "_retail_", *self.args)
        self.assertEqual(code, 0)
        self.assertIn("linked", out)
        self.assertTrue(self.dest().is_symlink())

    def test_push_targets_installed_copies(self):
        args = type("A", (), {"addon_data": None, "wow_dir": [str(self.wow)]})()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(goldmine.addon_data_targets(args), [])
        cli("addon", "install", "--flavor", "_retail_", "--flavor", "_classic_", *self.args)
        self.assertEqual(goldmine.addon_data_targets(args),
                         [self.dest("_classic_") / "Data.lua", self.dest("_retail_") / "Data.lua"])

    def test_rejects_unsafe_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Goldmine/../../evil.lua", "x")
        with self.assertRaises(addon.AddonError):
            addon.install(buf.getvalue(), self.wow / "_retail_")
        self.assertFalse((self.wow / "_retail_" / "Interface" / "evil.lua").exists())


if __name__ == "__main__":
    unittest.main()
