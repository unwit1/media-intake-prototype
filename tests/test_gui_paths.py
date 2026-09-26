import tempfile
import unittest
from pathlib import Path

from media_intake.gui import _default_data_root


class PortableDataPathTest(unittest.TestCase):
    def test_portable_build_keeps_data_beside_the_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / 'MediaIntake.exe'
            self.assertEqual(_default_data_root(True, executable), Path(tmp) / 'MediaIntakeData')

    def test_source_checkout_uses_user_data_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_default_data_root(False, home=tmp), Path(tmp) / 'MediaIntakeData')


if __name__ == '__main__':
    unittest.main()
