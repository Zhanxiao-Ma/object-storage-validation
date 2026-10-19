"""Check runtime assembly using production file linking and independent raw file expectations."""
from pathlib import Path
import tempfile
import unittest

from scripts.obs_runtime import prepare


def raw_sdk(root):
    directories = [root/"build/script/Provider/build/linux/example",
                   root/"platform/libboundscheck/lib/linux", root/"platform/eSDK_LogAPI_V2.1.10/C/linux_64"]
    for directory in directories:
        directory.mkdir(parents=True)
    library = root/"libeSDKOBS.so"
    library.write_bytes(b"raw-sdk-library-input")
    (directories[0]/"libtransport.so.1").write_bytes(b"raw-transport-input")
    return library, directories


class ObsRuntimeTest(unittest.TestCase):
    """Validate isolated dependency output and prevent overwriting unrelated runtime files."""

    def test_real_symlinks_reach_original_files(self):
        """Feed raw dependency files into the production assembler; expect links resolving to exactly the original bytes."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library, _ = raw_sdk(root/"sdk")
            destination = root/"runtime"
            self.assertEqual(prepare(root/"sdk", library, destination, "x86_64"), 2)
            self.assertTrue((destination/"libeSDKOBS.so").is_symlink())
            self.assertEqual((destination/"libeSDKOBS.so").read_bytes(), b"raw-sdk-library-input")
            self.assertEqual((destination/"libtransport.so.1").read_bytes(), b"raw-transport-input")

    def test_existing_destination_and_ambiguous_libraries_are_rejected(self):
        """Provide an existing output or conflicting soname inputs; expect rejection and preservation of existing bytes."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library, directories = raw_sdk(root/"sdk")
            destination = root/"runtime"
            destination.mkdir()
            (destination/"keep").write_bytes(b"untouched")
            with self.assertRaises(FileExistsError):
                prepare(root/"sdk", library, destination, "x86_64")
            self.assertEqual((destination/"keep").read_bytes(), b"untouched")
            (directories[1]/"libtransport.so.1").write_bytes(b"conflicting-original")
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                prepare(root/"sdk", library, root/"other-runtime", "x86_64")
            self.assertFalse((root/"other-runtime").exists())

    def test_missing_dependencies_and_unknown_architecture_are_rejected(self):
        """Omit SDK dependencies or select an unsupported architecture; expect failure before output creation."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for architecture in ("x86_64", "unsupported"):
                with self.subTest(architecture=architecture), self.assertRaises(ValueError):
                    prepare(root, root/"missing-library", root/"output", architecture)
            self.assertFalse((root/"output").exists())


if __name__ == "__main__":
    unittest.main()
