"""Tests for atomicio.atomic_write — crash-safe file writes.

Pure stdlib; exercises temp-file cleanup, parent creation, byte payloads,
overwrite semantics, and that a failed replace leaves the target intact.
"""
import importlib
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

atomicio = importlib.import_module("atomicio")


class TestAtomicWrite(unittest.TestCase):
    def test_writes_str_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "out.json"
            atomicio.atomic_write(p, '{"a": 1}')
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": 1})

    def test_writes_bytes_payload(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "blob.bin"
            atomicio.atomic_write(p, b"\x00\x01\xff")
            self.assertEqual(p.read_bytes(), b"\x00\x01\xff")

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "a" / "b" / "c.json"
            atomicio.atomic_write(p, "{}")
            self.assertTrue(p.exists())

    def test_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "out.json"
            atomicio.atomic_write(p, "x")
            leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "out.json"
            atomicio.atomic_write(p, "first")
            atomicio.atomic_write(p, "second")
            self.assertEqual(p.read_text(encoding="utf-8"), "second")

    def test_failed_replace_keeps_target_and_cleans_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "out.json"
            p.write_text("original", encoding="utf-8")
            real_replace = os.replace

            def boom(src, dst):
                if pathlib.Path(dst).name == "out.json":
                    raise OSError("boom")
                return real_replace(src, dst)

            with patch("atomicio.os.replace", side_effect=boom), self.assertRaises(OSError):
                atomicio.atomic_write(p, "never lands")
            self.assertEqual(p.read_text(encoding="utf-8"), "original")
            leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_path_input_as_str(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "out.json")
            atomicio.atomic_write(p, "s")
            self.assertEqual(pathlib.Path(p).read_text(encoding="utf-8"), "s")


if __name__ == "__main__":
    unittest.main()
