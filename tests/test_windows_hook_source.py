"""Source guards for the Windows hook, which cannot be imported off Windows.

Both crash reports in issues #252 and #253 trace to one line: ``dwExtraInfo``
is a ULONG_PTR *value*, but the struct declared it as a pointer and the debug
path read ``.contents``, dereferencing whatever number the sender attached.
Any event carrying a non-zero value killed the process outright -- silently,
with nothing in the log -- whenever debug mode was on.
"""

from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_HOOK = (ROOT / "core" / "mouse_hook_windows.py").read_text(encoding="utf-8")
KEY_CAPTURE = (ROOT / "core" / "key_capture.py").read_text(encoding="utf-8")


class ExtraInfoIsAValueNotAPointerTests(unittest.TestCase):
    def test_mouse_hook_struct_declares_extra_info_as_a_value(self):
        self.assertIn('("dwExtraInfo", wintypes.WPARAM)', WINDOWS_HOOK)

    def test_key_capture_struct_declares_extra_info_as_a_value(self):
        self.assertIn('("dwExtraInfo", wintypes.WPARAM)', KEY_CAPTURE)

    def test_no_hook_dereferences_extra_info(self):
        for name, source in (
            ("mouse_hook_windows", WINDOWS_HOOK),
            ("key_capture", KEY_CAPTURE),
        ):
            with self.subTest(module=name):
                self.assertNotIn("dwExtraInfo.contents", source)


class DebugLoggingIsRateLimitedTests(unittest.TestCase):
    def test_wheel_bursts_are_coalesced(self):
        # A hi-res wheel emits ~15 messages per detent; one debug line each
        # costs a Qt signal plus a QML list rebuild.
        self.assertIn("_DEBUG_BURST_MESSAGES", WINDOWS_HOOK)
        self.assertIn("def _debug_event_allowed", WINDOWS_HOOK)

    def test_debug_path_skips_mouse_moves_before_formatting(self):
        self.assertIn(
            "if self.debug_mode and self._debug_callback and wParam != WM_MOUSEMOVE:",
            WINDOWS_HOOK,
        )


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(sys.platform == "win32", "Windows hook only imports on Windows")
class ResumeRecoveryTests(unittest.TestCase):
    """Resume from sleep re-probes a disconnected listener right away and
    asks a connected one to read its settings back; the AUTOMATIC/SUSPEND
    pair a single wake raises collapses into one pass."""

    def test_resume_notifies_listener_once_per_wake(self):
        from core import mouse_hook_windows as mhw

        hook = mhw.MouseHook()
        hook._hid_gesture = Mock()
        with patch("builtins.print"):
            self.assertTrue(hook._on_resume())
            self.assertFalse(hook._on_resume())  # PBT_APMRESUMESUSPEND echo
        hook._hid_gesture.notify_device_change.assert_called_once_with()
        hook._hid_gesture.request_state_verify.assert_called_once_with()

    def test_resume_without_listener_is_harmless(self):
        from core import mouse_hook_windows as mhw

        hook = mhw.MouseHook()
        with patch("builtins.print"):
            self.assertFalse(hook._on_resume())


@unittest.skipUnless(sys.platform == "win32", "Windows hook")
class PrecisionDragWindowsTests(unittest.TestCase):
    """WH_MOUSE_LL feeds primary-button and modifier samples (GetAsyncKeyState)
    to the base state machine through the precision worker queue."""

    def setUp(self):
        from core import mouse_hook_windows as mhw

        self.mhw = mhw
        self.hook = mhw.MouseHook()
        self.hook._connected_device = object()
        self.hook._hid_gesture = Mock()
        self.hook._hid_gesture.shift_dpi.return_value = True
        self.hook.configure_precision_drag(True, "alt", 300, lambda: 3600)
        self.held = set()
        patcher = patch.object(
            mhw, "GetAsyncKeyState",
            side_effect=lambda vk: 0x8000 if vk in self.held else 0,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        next_patcher = patch.object(mhw, "CallNextHookEx", return_value=0)
        next_patcher.start()
        self.addCleanup(next_patcher.stop)

    def _send(self, wParam, flags=0):
        data = self.mhw.MSLLHOOKSTRUCT()
        data.flags = flags
        lParam = self.mhw.ctypes.pointer(data)
        with patch("builtins.print"):
            self.hook._low_level_handler_inner(self.mhw.HC_ACTION, wParam, lParam)
            self._drain()

    def _drain(self):
        while not self.hook._precision_queue.empty():
            self.hook._process_precision_item(self.hook._precision_queue.get_nowait())

    def test_alt_drag_shifts_and_release_restores(self):
        self.held.add(self.mhw.VK_MENU)
        self._send(self.mhw.WM_LBUTTONDOWN)
        self.hook._hid_gesture.shift_dpi.assert_called_once_with(300)
        self._send(self.mhw.WM_MOUSEMOVE)
        self._send(self.mhw.WM_LBUTTONUP)
        self.hook._hid_gesture.unshift_dpi.assert_called_once_with(3600)

    def test_other_modifier_does_not_count(self):
        self.held.add(self.mhw.VK_CONTROL)
        self._send(self.mhw.WM_LBUTTONDOWN)
        self.hook._hid_gesture.shift_dpi.assert_not_called()

    def test_modifier_pressed_during_drag_shifts_and_released_restores(self):
        self._send(self.mhw.WM_LBUTTONDOWN)
        self._send(self.mhw.WM_MOUSEMOVE)
        self.hook._hid_gesture.shift_dpi.assert_not_called()
        self.held.add(self.mhw.VK_MENU)
        self._send(self.mhw.WM_MOUSEMOVE)
        self.hook._hid_gesture.shift_dpi.assert_called_once_with(300)
        self.held.clear()
        self._send(self.mhw.WM_MOUSEMOVE)
        self.hook._hid_gesture.unshift_dpi.assert_called_once_with(3600)

    def test_mac_names_and_win_key_map_to_pc_keys(self):
        for modifier, vk in (
            ("command", self.mhw.VK_CONTROL),
            ("option", self.mhw.VK_MENU),
            ("control", self.mhw.VK_CONTROL),
            ("win", self.mhw.VK_RWIN),
            ("shift", self.mhw.VK_SHIFT),
        ):
            self.hook.configure_precision_drag(True, modifier, 300, lambda: 3600)
            self.held = {vk}
            self.assertTrue(self.hook._precision_modifier_down(), modifier)
        self.hook.configure_precision_drag(True, "function", 300, lambda: 3600)
        self.held = {self.mhw.VK_CONTROL, self.mhw.VK_MENU}
        self.assertFalse(self.hook._precision_modifier_down())

    def test_injected_events_are_ignored(self):
        self.held.add(self.mhw.VK_MENU)
        self._send(self.mhw.WM_LBUTTONDOWN, flags=self.mhw.INJECTED_FLAG)
        self.hook._hid_gesture.shift_dpi.assert_not_called()

    def test_hook_callback_never_writes_hid_itself(self):
        # The write happens on the worker: nothing reaches HID++ until the
        # queue is processed, and the hook thread only samples key state.
        self.held.add(self.mhw.VK_MENU)
        data = self.mhw.MSLLHOOKSTRUCT()
        self.hook._low_level_handler_inner(
            self.mhw.HC_ACTION, self.mhw.WM_LBUTTONDOWN, self.mhw.ctypes.pointer(data)
        )
        self.hook._hid_gesture.shift_dpi.assert_not_called()
        self._drain()
        self.hook._hid_gesture.shift_dpi.assert_called_once_with(300)

    def test_rehook_ends_a_drag_whose_release_may_be_lost(self):
        self.held.add(self.mhw.VK_MENU)
        self._send(self.mhw.WM_LBUTTONDOWN)
        with (
            patch.object(self.mhw, "UnhookWindowsHookEx"),
            patch.object(self.mhw, "SetWindowsHookExW", return_value=1),
            patch("builtins.print"),
        ):
            self.hook._reinstall_hook()
        self._drain()
        self.hook._hid_gesture.unshift_dpi.assert_called_once_with(3600)
        self.assertFalse(self.hook._precision_button_down)

    def test_stop_restores_dpi_before_the_hid_listener_goes_away(self):
        self.held.add(self.mhw.VK_MENU)
        self._send(self.mhw.WM_LBUTTONDOWN)
        order = []
        with (
            patch.object(self.hook, "_stop_hid_listener",
                         side_effect=lambda: order.append("hid")),
            patch.object(self.hook._hid_gesture, "unshift_dpi",
                         side_effect=lambda _dpi: order.append("unshift")),
            patch("builtins.print"),
        ):
            self.hook.stop()
        self.assertEqual(order, ["unshift", "hid"])

    def test_worker_thread_processes_samples_in_order(self):
        self.hook._start_precision_worker()
        try:
            self.held.add(self.mhw.VK_MENU)
            self.hook._precision_queue.put(("button", True, True))
            self.hook._precision_queue.put(("modifier", False))
            deadline = time.time() + 2
            while time.time() < deadline and not self.hook._hid_gesture.unshift_dpi.called:
                time.sleep(0.01)
        finally:
            self.hook._stop_precision_worker()
        self.hook._hid_gesture.shift_dpi.assert_called_once_with(300)
        self.hook._hid_gesture.unshift_dpi.assert_called_once_with(3600)
        self.assertIsNone(self.hook._precision_thread)
