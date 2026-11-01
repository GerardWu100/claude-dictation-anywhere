"""Launchers: the Linux entry and the macOS app, built here with stand-ins for macOS's own tools."""

import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from claude_dictation_anywhere import launcher

REAL_RUN = subprocess.run  # the tests below replace subprocess.run for the launcher


class MacTools:
    """Stands in for osacompile and codesign, and records the order they ran in."""

    def __init__(self):
        self.calls = []
        self.source = ""
        self.fail_on = None
        self.info_when_signed = None

    def run(self, args, **_):
        self.calls.append(args[0])
        if args[0] == self.fail_on:
            raise subprocess.CalledProcessError(1, args[0])
        if args[0] == "osacompile":
            self.source = args[args.index("-e") + 1]
            bundle = Path(args[args.index("-o") + 1])
            resources = bundle / "Contents" / "Resources"
            resources.mkdir(parents=True)
            (resources / "Assets.car").write_bytes(b"stock icon")
            with (bundle / "Contents" / "Info.plist").open("wb") as file:
                plistlib.dump({"CFBundleName": "applet", "CFBundleIconName": "applet"}, file)
        elif args[0] == "codesign":
            with (Path(args[-1]) / "Contents" / "Info.plist").open("rb") as file:
                self.info_when_signed = plistlib.load(file)
        return subprocess.CompletedProcess(args, 0)


class MacAppTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.tools = MacTools()
        patches = [
            patch.object(launcher, "IS_MAC", True),
            patch.object(launcher.Path, "home", return_value=self.home),
            patch.object(launcher.subprocess, "run", self.tools.run),
            patch.object(launcher, "app_command", return_value="/Users/me/.local/bin/cda"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.bundle = self.home / "Applications" / "Claude Dictation Anywhere.app"

    def info(self):
        with (self.bundle / "Contents" / "Info.plist").open("rb") as file:
            return plistlib.load(file)

    def test_app_can_ask_for_the_microphone_and_stays_out_of_the_dock(self):
        launcher.install_launcher()
        info = self.info()
        self.assertTrue(info["NSMicrophoneUsageDescription"])
        self.assertIs(info["LSUIElement"], True)
        self.assertEqual(info["CFBundleName"], "Claude Dictation Anywhere")
        self.assertTrue(info["CFBundleIdentifier"])

    def test_signing_comes_last_so_the_edits_are_covered(self):
        launcher.install_launcher()
        self.assertEqual(self.tools.calls, ["osacompile", "codesign"])
        self.assertEqual(self.tools.info_when_signed, self.info())

    def test_icon_is_a_real_icns_and_replaces_the_stock_one(self):
        launcher.install_launcher()
        resources = self.bundle / "Contents" / "Resources"
        with Image.open(resources / "applet.icns") as icon:
            self.assertEqual(icon.format, "ICNS")
        self.assertFalse((resources / "Assets.car").exists())
        self.assertNotIn("CFBundleIconName", self.info())

    def test_applet_runs_the_installed_command_and_stays_open(self):
        launcher.install_launcher()
        self.assertIn("do shell script", self.tools.source)
        self.assertIn("exec /Users/me/.local/bin/cda --open", self.tools.source)
        self.assertNotIn(" &", self.tools.source)  # in the background it would not hold permissions

    def test_applet_finds_homebrew_and_uses_utf_8(self):
        launcher.install_launcher()
        self.assertIn("/opt/homebrew/bin", self.tools.source)
        self.assertIn("UTF-8", self.tools.source)

    def test_quotes_and_spaces_in_the_path_survive_both_languages(self):
        source = launcher.mac_applet_source('/Users/a b/say "hi"/cda')
        shell = source.split("do shell script ", 1)[1].split("\n", 1)[0]
        self.assertTrue(shell.startswith('"') and shell.endswith('"'))
        body = shell[1:-1]
        self.assertNotRegex(body.replace('\\"', "").replace("\\\\", ""), r'["\\]')
        unquoted = body.replace('\\"', '"').replace("\\\\", "\\")
        result = REAL_RUN(
            ["sh", "-c", unquoted.split("exec ", 1)[1].replace(" --open >/dev/null 2>&1", "")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertIn("/Users/a b/say", result.stderr)  # one word for sh, quotes intact

    def test_failed_build_leaves_an_earlier_app_and_no_temporary_files(self):
        launcher.install_launcher()
        before = self.info()
        for failing in ("osacompile", "codesign"):
            self.tools.fail_on = failing
            with self.subTest(failing), self.assertRaises(SystemExit):
                launcher.install_launcher()
            self.assertEqual(self.info(), before)
            self.assertEqual(os.listdir(self.home / "Applications"), [self.bundle.name])

    def test_reinstall_replaces_and_uninstall_removes(self):
        launcher.install_launcher()
        launcher.install_launcher()
        self.assertTrue(self.bundle.is_dir())
        launcher.uninstall_launcher()
        self.assertFalse(self.bundle.exists())
        launcher.uninstall_launcher()  # safe to repeat


class LinuxEntryTest(unittest.TestCase):
    def test_entry_runs_the_command_with_open_and_no_terminal(self):
        entry = launcher.desktop_entry("/home/me/.local/bin/cda")
        self.assertIn('Exec="/home/me/.local/bin/cda" --open', entry)
        self.assertIn("Terminal=false", entry)

    def test_install_and_uninstall_stay_in_the_data_folder(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"XDG_DATA_HOME": folder}),
            patch.object(launcher, "IS_MAC", False),
        ):
            launcher.install_launcher()
            self.assertTrue(launcher.desktop_file_path().is_file())
            self.assertTrue(launcher.icon_path().is_file())
            self.assertTrue(stat.S_ISREG(launcher.icon_path().stat().st_mode))
            launcher.uninstall_launcher()
            self.assertFalse(launcher.desktop_file_path().exists())


if __name__ == "__main__":
    unittest.main()
