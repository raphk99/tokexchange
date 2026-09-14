import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from tokexchange import bundle


class BundleTests(unittest.TestCase):
    def test_pack_unpack(self):
        data = bundle.pack({"task.json": b"{}", "files/a/b.txt": b"hello"})
        self.assertEqual(set(bundle.list_members(data)), {"task.json", "files/a/b.txt"})
        self.assertEqual(bundle.read_json_member(data, "task.json"), {})
        with tempfile.TemporaryDirectory() as d:
            written = bundle.unpack(data, Path(d))
            self.assertEqual((Path(d) / "files/a/b.txt").read_text(), "hello")
            self.assertEqual(sorted(written), ["files/a/b.txt", "task.json"])

    def test_rejects_traversal_on_pack(self):
        with self.assertRaises(bundle.BundleError):
            bundle.pack({"../evil": b"x"})
        with self.assertRaises(bundle.BundleError):
            bundle.pack({"/abs": b"x"})

    def test_rejects_traversal_and_symlinks_on_unpack(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("../../evil.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(bundle.BundleError):
                bundle.unpack(buf.getvalue(), Path(d))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(bundle.BundleError):
                bundle.unpack(buf.getvalue(), Path(d))
