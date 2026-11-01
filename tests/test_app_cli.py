"""The command line: argument checks, log files, and running without a desktop."""

import argparse
import math
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from claude_dictation_anywhere import app


class TestDurationTest(unittest.TestCase):
    def test_refuses_what_would_not_be_one_bounded_recording(self):
        for value in ("0", "-1", "nan", "inf", "-inf", str(app.MAX_TEST_SECONDS + 1), "1e9"):
            with self.subTest(value), self.assertRaises(argparse.ArgumentTypeError):
                app.test_duration(value)

    def test_accepts_a_normal_value(self):
        self.assertEqual(app.test_duration("5"), 5.0)
        self.assertTrue(math.isclose(app.test_duration("0.5"), 0.5))

    def test_command_line_rejects_zero_instead_of_starting_the_app(self):
        result = subprocess.run(
            [sys.executable, "-m", "claude_dictation_anywhere", "--test", "0"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be above 0", result.stderr)


class NoDesktopTest(unittest.TestCase):
    def test_help_works_and_loads_no_tray_or_input_code(self):
        code = (
            "import sys\n"
            "sys.argv = ['claude-dictation-anywhere', '--help']\n"
            "from claude_dictation_anywhere import app, launcher\n"
            "banned = {'pystray', 'evdev', 'gi'} & set(sys.modules)\n"
            "assert not banned, banned\n"
            "from claude_dictation_anywhere.icon import draw_icon\n"
            "assert draw_icon('failed').size == (64, 64)\n"
            "try:\n"
            "    app.main()\n"
            "except SystemExit as stop:\n"
            "    assert stop.code == 0\n"
            "banned = {'pystray', 'evdev', 'gi'} & set(sys.modules)\n"
            "assert not banned, banned\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--test", result.stdout)

    def test_web_page_is_packaged(self):
        from importlib import resources

        page = resources.files("claude_dictation_anywhere").joinpath("web/index.html")
        self.assertIn("__CDA_TOKEN__", page.read_text(encoding="utf-8"))


class LogFilesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.logs = Path(tmp.name) / "logs"
        environment = patch.object(app, "data_dir", return_value=Path(tmp.name))
        environment.start()
        self.addCleanup(environment.stop)
        self.today = date.today().isoformat()

    def test_malformed_names_are_ignored(self):
        self.logs.mkdir()
        for name in (
            f"{self.today}_abc.log",
            f"{self.today}.log",
            "notes.log",
            f"{self.today}_.log",
        ):
            (self.logs / name).touch()
        (self.logs / f"{self.today}_004.log").touch()
        self.assertEqual(app.next_log_path().name, f"{self.today}_005.log")
        self.assertTrue((self.logs / "notes.log").exists())

    def test_first_log_of_the_day(self):
        self.assertEqual(app.next_log_path().name, f"{self.today}_001.log")
        self.assertEqual(stat.S_IMODE(self.logs.stat().st_mode), 0o700)

    def test_oldest_logs_are_removed_and_the_count_stays_bounded(self):
        self.logs.mkdir()
        for day in range(1, app.MAX_LOG_FILES + 6):
            (self.logs / f"2020-01-{day:02d}_001.log").touch()
        app.next_log_path()
        kept = sorted(p.name for p in self.logs.iterdir())
        self.assertEqual(len(kept), app.MAX_LOG_FILES - 1)
        self.assertNotIn("2020-01-01_001.log", kept)
        self.assertIn(f"2020-01-{app.MAX_LOG_FILES + 5:02d}_001.log", kept)

    def test_log_file_is_private(self):
        path = app.next_log_path()
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        handler = app.open_private_log(path)
        self.addCleanup(handler.close)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
