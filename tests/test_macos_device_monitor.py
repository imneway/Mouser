"""Tests for the macOS Logitech device-change monitor and its hook wiring."""

import importlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import core
from core import macos_device_monitor


class MacOSHookDeviceChangeTests(unittest.TestCase):
    """The macOS hook forwards IOKit device changes to the HID++ listener,
    mirroring the Windows WM_DEVICECHANGE path."""

    def setUp(self):
        self._module_name = "core.mouse_hook_macos"
        self._loaded_test_module = False
        self.module = sys.modules.get(self._module_name)
        if self.module is None:
            fake_objc = SimpleNamespace(autorelease_pool=MagicMock())
            fake_quartz = MagicMock(name="Quartz")
            with patch.dict(sys.modules, {"objc": fake_objc, "Quartz": fake_quartz}):
                self.module = importlib.import_module(self._module_name)
            self._loaded_test_module = True
            self.addCleanup(self._unload_module)

        self.hook = self.module.MouseHook()
        self.hook._running = True
        self.listener = Mock()
        self.hook._hid_gesture = self.listener

    def _unload_module(self):
        if self._loaded_test_module and sys.modules.get(self._module_name) is self.module:
            sys.modules.pop(self._module_name, None)
        if self._loaded_test_module and getattr(core, "mouse_hook_macos", None) is self.module:
            delattr(core, "mouse_hook_macos")

    def test_device_arrival_cuts_listener_backoff_short(self):
        with patch("builtins.print"):
            self.hook._on_logitech_device_change(True)
        self.listener.notify_device_change.assert_called_once_with()

    def test_device_removal_also_notifies_listener(self):
        with patch("builtins.print"):
            self.hook._on_logitech_device_change(False)
        self.listener.notify_device_change.assert_called_once_with()

    def test_burst_of_interface_notifications_logs_once(self):
        with (
            patch.object(self.module.time, "monotonic", side_effect=(100.0, 100.1, 100.2)),
            patch("builtins.print") as print_mock,
        ):
            for _ in range(3):
                self.hook._on_logitech_device_change(True)
        self.assertEqual(self.listener.notify_device_change.call_count, 3)
        self.assertEqual(print_mock.call_count, 1)

    def test_ignored_after_stop_and_without_listener(self):
        self.hook._running = False
        self.hook._on_logitech_device_change(True)
        self.listener.notify_device_change.assert_not_called()

        self.hook._running = True
        self.hook._hid_gesture = None
        with patch("builtins.print"):
            self.hook._on_logitech_device_change(True)  # must not raise

    def test_listener_failure_is_contained(self):
        self.listener.notify_device_change.side_effect = RuntimeError("boom")
        with patch("builtins.print"):
            self.hook._on_logitech_device_change(True)  # must not raise

    def test_monitor_lifecycle_is_owned_by_the_hook(self):
        monitor = Mock()
        monitor.start.return_value = True
        with patch.object(
            macos_device_monitor, "LogitechDeviceMonitor", return_value=monitor
        ) as cls:
            self.hook._start_device_monitor()
            self.hook._start_device_monitor()  # idempotent
        cls.assert_called_once_with(self.hook._on_logitech_device_change)
        monitor.start.assert_called_once_with()
        self.assertIs(self.hook._device_monitor, monitor)

        self.hook._stop_device_monitor()
        monitor.stop.assert_called_once_with()
        self.assertIsNone(self.hook._device_monitor)

    def test_monitor_that_fails_to_start_is_not_kept(self):
        monitor = Mock()
        monitor.start.return_value = False
        with patch.object(
            macos_device_monitor, "LogitechDeviceMonitor", return_value=monitor
        ):
            self.hook._start_device_monitor()
        self.assertIsNone(self.hook._device_monitor)
        monitor.stop.assert_called_once_with()


class MacOSMotionWakesReconnectTests(unittest.TestCase):
    """Pointer motion must reach the HID++ listener while no Logitech is
    bound -- that motion is what cuts the reconnect backoff short after a
    KVM / Easy-Switch return."""

    def setUp(self):
        self._module_name = "core.mouse_hook_macos"
        self._saved_module = sys.modules.pop(self._module_name, None)
        self._saved_attr = getattr(core, "mouse_hook_macos", None)
        fake_objc = SimpleNamespace(autorelease_pool=MagicMock())
        fake_quartz = MagicMock(name="Quartz")
        with patch.dict(sys.modules, {"objc": fake_objc, "Quartz": fake_quartz}):
            self.module = importlib.import_module(self._module_name)
        self.addCleanup(self._restore)
        self.quartz = self.module.Quartz
        self.quartz.CGEventGetIntegerValueField.return_value = 0
        self.hook = self.module.MouseHook()
        self.hook._first_event_logged = True
        self.listener = Mock()
        self.hook._hid_gesture = self.listener

    def _restore(self):
        sys.modules.pop(self._module_name, None)
        if self._saved_module is not None:
            sys.modules[self._module_name] = self._saved_module
        if self._saved_attr is not None:
            core.mouse_hook_macos = self._saved_attr
        elif getattr(core, "mouse_hook_macos", None) is self.module:
            delattr(core, "mouse_hook_macos")

    def test_motion_while_disconnected_wakes_the_listener_and_passes_through(self):
        self.hook._connected_device = None
        result = self.hook._event_tap_callback(
            None, self.quartz.kCGEventMouseMoved, "evt", None
        )
        self.assertEqual(result, "evt")
        self.listener.notify_user_input.assert_called_once_with()


class LogitechDeviceMonitorTests(unittest.TestCase):
    def test_start_reports_failure_off_macos(self):
        with patch.object(macos_device_monitor, "_load_native", return_value=None):
            monitor = macos_device_monitor.LogitechDeviceMonitor(Mock())
            self.assertFalse(monitor.start())
            monitor.stop()

    @unittest.skipUnless(sys.platform == "darwin", "IOKit is macOS-only")
    def test_real_iokit_registration_starts_and_stops_cleanly(self):
        on_change = Mock()
        monitor = macos_device_monitor.LogitechDeviceMonitor(on_change)
        with patch("builtins.print"):
            self.assertTrue(monitor.start())
            self.assertTrue(monitor.running)
            thread = monitor._thread
            monitor.stop()
        self.assertFalse(thread.is_alive())
        # Devices already present at start-up are not reported as changes.
        on_change.assert_not_called()


if __name__ == "__main__":
    unittest.main()
