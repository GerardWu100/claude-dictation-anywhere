"""Check that macOS tray changes are dispatched to the main event loop."""

import unittest
from unittest.mock import Mock, patch

from claude_dictation_anywhere import settings


@unittest.skipUnless(settings.IS_MAC, "macOS tray")
class MacTrayTest(unittest.TestCase):
    def test_worker_refresh_and_stop_are_queued(self):
        from claude_dictation_anywhere import tray

        indicator = object.__new__(tray.TrayIcon)
        indicator.icon = Mock()
        controller = Mock(state="idle")
        with (
            patch.object(tray.threading, "current_thread", return_value=object()),
            patch.object(tray, "callAfter") as schedule,
        ):
            indicator.refresh(controller)
            schedule.assert_called_once_with(indicator.refresh, controller)
            indicator.icon.update_menu.assert_not_called()
            schedule.reset_mock()
            indicator.stop()
            schedule.assert_called_once_with(indicator.stop)
            indicator.icon.stop.assert_not_called()

    def test_visibility_setup_is_queued(self):
        from claude_dictation_anywhere import tray

        indicator = object.__new__(tray.TrayIcon)
        indicator.icon = Mock()
        with patch.object(tray, "callAfter") as schedule:
            indicator.run()
            setup = indicator.icon.run.call_args.kwargs["setup"]
            setup(indicator.icon)
            schedule.assert_called_once_with(setattr, indicator.icon, "visible", True)
