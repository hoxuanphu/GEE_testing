from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import start_workspace


class WindowsLauncherTests(unittest.TestCase):
    def test_lfs_pointers_have_an_actionable_error_before_install(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for relative in start_workspace.DATA_FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'placeholder data')
            start_workspace.check_boundary_files(root)
            (root / start_workspace.DATA_FILES[0]).write_bytes(b'version https://git-lfs.github.com/spec/v1\n')
            with self.assertRaisesRegex(start_workspace.SetupError, 'Git LFS'):
                start_workspace.check_boundary_files(root)

    def test_offline_install_uses_pinned_wheels_and_restart_reuses_environment(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'requirements-windows-py312.lock').write_bytes(b'example==1.0 --hash=sha256:example\n')
            (root / 'wheelhouse').mkdir()
            python = root / '.venv' / 'Scripts' / 'python.exe'
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(start_workspace, 'run_checked') as run, redirect_stdout(StringIO()):
                self.assertEqual(start_workspace.prepare_environment(root), python)
                install = run.call_args_list[1].args[0]
                self.assertIn('--no-index', install)
                self.assertIn('--require-hashes', install)
                self.assertIn('--no-cache-dir', install)
                self.assertEqual(run.call_args_list[-1].args[0][-2:], ['pip', 'check'])
                run.reset_mock()
                self.assertEqual(start_workspace.prepare_environment(root), python)
                run.assert_not_called()
