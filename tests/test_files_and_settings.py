"""Settings files, the runtime folder, and history, in temporary folders."""

import json
import os
import stat
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_dictation_anywhere import history, settings


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


class FolderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        previous = os.umask(0o022)  # the common default, which makes new files readable by others
        self.addCleanup(os.umask, previous)


class SaveSettingsTest(FolderCase):
    def test_concurrent_saves_do_not_collide(self):
        path = self.root / "settings.toml"
        errors = []
        start = threading.Barrier(8)

        def save(value):
            start.wait()
            try:
                for _ in range(20):
                    settings.save_settings(dict(settings.DEFAULT_SETTINGS, mode=value), path)
            except OSError as error:
                errors.append(error)

        threads = [
            threading.Thread(target=save, args=("toggle" if i % 2 else "hold",)) for i in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertIn(tomllib.loads(path.read_text())["mode"], ("toggle", "hold"))
        self.assertEqual([p.name for p in self.root.iterdir()], ["settings.toml"])

    def test_saved_file_is_private_also_after_replacing_an_open_one(self):
        path = self.root / "config" / "settings.toml"
        settings.save_settings(dict(settings.DEFAULT_SETTINGS), path)
        self.assertEqual(mode_of(path), 0o600)
        self.assertEqual(mode_of(path.parent), 0o700)
        path.chmod(0o644)
        settings.save_settings(dict(settings.DEFAULT_SETTINGS), path)
        self.assertEqual(mode_of(path), 0o600)

    def test_failed_save_leaves_the_old_file_and_no_leftovers(self):
        path = self.root / "settings.toml"
        settings.save_settings(dict(settings.DEFAULT_SETTINGS, mode="toggle"), path)
        with (
            patch.object(settings.os, "replace", side_effect=PermissionError(13, "no")),
            self.assertRaises(PermissionError),
        ):
            settings.save_settings(dict(settings.DEFAULT_SETTINGS), path)
        self.assertEqual(settings.load_settings(path)["mode"], "toggle")
        self.assertEqual([p.name for p in self.root.iterdir()], ["settings.toml"])


class ReadSettingsTest(FolderCase):
    def test_malformed_file_is_kept_aside_and_defaults_are_used(self):
        path = self.root / "settings.toml"
        path.write_text('mode = "toggle\nbroken')
        loaded, problems = settings.read_settings(path)
        self.assertEqual(loaded, settings.DEFAULT_SETTINGS)
        self.assertEqual(len(problems), 1)
        self.assertFalse(path.exists())
        kept = next(self.root.glob("settings.toml.broken-*"))
        self.assertEqual(kept.read_text(), 'mode = "toggle\nbroken')
        self.assertIn(str(kept), problems[0])

    def test_warning_stays_until_the_kept_file_is_dealt_with(self):
        path = self.root / "settings.toml"
        path.write_text("broken = = =")
        settings.read_settings(path)
        settings.save_settings(dict(settings.DEFAULT_SETTINGS), path)  # a later change in the app
        kept = next(self.root.glob("settings.toml.broken-*"))
        self.assertEqual(kept.read_text(), "broken = = =")  # not overwritten
        self.assertEqual(len(settings.read_settings(path)[1]), 1)
        kept.unlink()
        self.assertEqual(settings.read_settings(path)[1], [])

    def test_invalid_utf8_is_handled_the_same_way(self):
        path = self.root / "settings.toml"
        path.write_bytes(b'mode = "\xff\xfe"\n')
        loaded, problems = settings.read_settings(path)
        self.assertEqual(loaded["mode"], settings.DEFAULT_SETTINGS["mode"])
        self.assertEqual(len(problems), 1)

    def test_one_bad_entry_keeps_the_good_ones_and_is_reported(self):
        path = self.root / "settings.toml"
        path.write_text('mode = "toggle"\nlanguage = "klingon"\n')
        loaded, problems = settings.read_settings(path)
        self.assertEqual(loaded["mode"], "toggle")
        self.assertEqual(loaded["language"], settings.DEFAULT_SETTINGS["language"])
        self.assertIn("language", problems[0])
        self.assertTrue(path.exists())

    def test_missing_file_is_no_problem(self):
        loaded, problems = settings.read_settings(self.root / "none.toml")
        self.assertEqual((loaded, problems), (settings.DEFAULT_SETTINGS, []))


class RuntimeDirTest(FolderCase):
    def use_base(self):
        return patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)})

    def test_new_folder_is_private(self):
        with self.use_base():
            self.assertEqual(mode_of(settings.runtime_dir()), 0o700)

    def test_own_folder_with_broad_permissions_is_tightened(self):
        with self.use_base():
            path = settings.runtime_dir()
            path.chmod(0o777)
            self.assertEqual(mode_of(settings.runtime_dir()), 0o700)

    def test_symlink_is_refused_and_its_target_left_alone(self):
        target = self.root / "elsewhere"
        target.mkdir(mode=0o755)
        (self.root / settings.APP_NAME).symlink_to(target)
        with self.use_base(), self.assertRaisesRegex(settings.UnsafeFolderError, "symlink"):
            settings.runtime_dir()
        self.assertEqual(mode_of(target), 0o755)

    def test_file_in_its_place_is_refused(self):
        (self.root / settings.APP_NAME).write_text("x")
        with self.use_base(), self.assertRaises(settings.UnsafeFolderError):
            settings.runtime_dir()

    def test_folder_of_another_user_is_refused_and_not_repaired(self):
        with self.use_base():
            path = settings.runtime_dir()
            path.chmod(0o777)
            real_uid = os.getuid()
            with (
                patch.object(settings.os, "getuid", return_value=real_uid + 1),
                self.assertRaisesRegex(settings.UnsafeFolderError, "another user"),
            ):
                settings.runtime_dir()
            self.assertEqual(mode_of(path), 0o777)

    def test_without_xdg_it_uses_the_users_cache_never_a_shared_temp_folder(self):
        env = {
            k: v for k, v in os.environ.items() if k not in ("XDG_RUNTIME_DIR", "XDG_CACHE_HOME")
        }
        env["HOME"] = str(self.root)
        with patch.dict(os.environ, env, clear=True):
            path = settings.runtime_dir()
        self.assertTrue(path.is_relative_to(self.root))
        self.assertNotIn(tempfile.gettempdir(), str(path).replace(str(self.root), ""))
        self.assertEqual(mode_of(path), 0o700)


class MakePrivateTest(FolderCase):
    def test_parents_are_left_alone(self):
        parent = self.root / "parent"
        parent.mkdir(mode=0o755)
        settings.make_private_dir(parent / "child")
        self.assertEqual(mode_of(parent), 0o755)
        self.assertEqual(mode_of(parent / "child"), 0o700)

    def test_symlinked_config_folder_keeps_working_untouched(self):
        target = self.root / "dotfiles"
        target.mkdir(mode=0o755)
        link = self.root / "link"
        link.symlink_to(target)
        settings.make_private_dir(link)
        self.assertEqual(mode_of(target), 0o755)

    def test_tighten_ignores_links_and_missing_files(self):
        target = self.root / "other"
        target.write_text("x")
        target.chmod(0o644)
        link = self.root / "link"
        link.symlink_to(target)
        settings.tighten_private_file(link)
        settings.tighten_private_file(self.root / "missing")
        self.assertEqual(mode_of(target), 0o644)
        settings.tighten_private_file(target)
        self.assertEqual(mode_of(target), 0o600)


class HistoryTest(FolderCase):
    def log(self, **kwargs):
        return history.History(self.root / "data" / "history.jsonl", **kwargs)

    def test_new_file_and_folder_are_private_and_stay_so_after_trimming(self):
        log = self.log(max_entries=2)
        for word in ("a", "b", "c"):
            log.add(word, 1.0)
        self.assertEqual(mode_of(log.path), 0o600)
        self.assertEqual(mode_of(log.path.parent), 0o700)
        self.assertEqual([e["text"] for e in log.page(1, 5)["entries"]], ["c", "b"])

    def test_existing_broad_file_is_tightened(self):
        log = self.log()
        log.path.parent.mkdir()
        log.path.write_text("")
        log.path.chmod(0o644)
        log.page(1, 1)["entries"]
        self.assertEqual(mode_of(log.path), 0o600)

    def test_damaged_and_wrong_shaped_lines_are_skipped(self):
        log = self.log()
        log.path.parent.mkdir()
        good = {"time": "2026-10-25T22:12:29", "text": "fine", "seconds": 1.5}
        lines = [
            b"cut sho",
            b"\xff\xfe not text",
            b"[1, 2]",
            b'"just a string"',
            json.dumps({"text": "no time", "seconds": 1}).encode(),
            json.dumps({"time": "t", "text": 5, "seconds": 1}).encode(),
            json.dumps({"time": "t", "text": "x", "seconds": "1"}).encode(),
            json.dumps(good).encode(),
        ]
        log.path.write_bytes(b"\n".join(lines) + b"\n")
        with self.assertLogs("claude_dictation_anywhere.history", "WARNING"):
            self.assertEqual(log.page(1, 10)["entries"], [good])

    def test_read_failure_shows_nothing_rather_than_crashing(self):
        log = self.log()
        log.path.parent.mkdir()
        log.path.mkdir()  # a folder where the file should be
        with self.assertLogs("claude_dictation_anywhere.history", "WARNING"):
            self.assertEqual(log.page(1, 5)["entries"], [])

    def test_add_reports_a_write_failure_to_the_caller(self):
        log = self.log()
        log.page(1, 1)["entries"]
        with (
            patch.object(history.os, "open", side_effect=OSError(28, "No space")),
            self.assertRaises(OSError),
        ):
            log.add("x", 1.0)

    def test_repeated_polls_do_not_reread_the_file(self):
        log = self.log()
        log.add("x", 1.0)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("read again")):
            self.assertEqual(log.page(1, 1)["entries"][0]["text"], "x")

    def test_clear_removes_the_file_and_the_memory(self):
        log = self.log()
        log.add("x", 1.0)
        log.clear()
        self.assertEqual(log.page(1, 5)["entries"], [])
        self.assertFalse(log.path.exists())

    def test_pages_run_from_newest_to_oldest_and_clamp(self):
        log = self.log()
        for word in "abcde":
            log.add(word, 1.0)
        texts = lambda page: [e["text"] for e in page["entries"]]
        first = log.page(1, 2)
        self.assertEqual((texts(first), first["pages"], first["total"]), (["e", "d"], 3, 5))
        self.assertEqual(texts(log.page(3, 2)), ["a"])
        self.assertEqual(log.page(9, 2)["page"], 3)  # past the end: the last page
        self.assertEqual(log.page(0, 2)["page"], 1)
        log.clear()
        self.assertEqual(log.page(2, 2), {"entries": [], "page": 1, "pages": 1, "total": 0})


if __name__ == "__main__":
    unittest.main()
