"""Precision drag: hold a modifier while dragging to drop the sensor to a low
DPI, restored on release. Covers the config rule resolution, the hook state
machine, the immediate HID++ writes, the engine wiring and the macOS tap."""

import copy
import importlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import core
from core import hid_gesture
from core.config import (
    DEFAULT_CONFIG,
    PRECISION_DRAG_DEFAULT_MODIFIER,
    get_global_precision_drag,
    get_precision_drag_for_profile,
    get_profile_precision_drag,
    set_profile_precision_drag,
)
from core.mouse_hook_base import BaseMouseHook


def _cfg(**settings):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["settings"].update(settings)
    cfg["profiles"]["figma"] = {"label": "Figma", "apps": ["Figma"], "mappings": {}}
    return cfg


class PrecisionDragConfigTests(unittest.TestCase):
    def test_defaults_are_off_platform_modifier_400(self):
        rule = get_global_precision_drag(copy.deepcopy(DEFAULT_CONFIG))
        self.assertEqual(
            rule, {"enabled": False, "modifier": PRECISION_DRAG_DEFAULT_MODIFIER, "dpi": 400}
        )
        # Command on macOS, Ctrl on Windows (where Command shortcuts live).
        self.assertEqual(
            PRECISION_DRAG_DEFAULT_MODIFIER,
            "command" if sys.platform == "darwin" else "control",
        )

    def test_bad_values_fall_back_to_defaults(self):
        cfg = _cfg(precision_drag_modifier="hyper", precision_drag_dpi="fast")
        rule = get_global_precision_drag(cfg)
        self.assertEqual(rule["modifier"], PRECISION_DRAG_DEFAULT_MODIFIER)
        self.assertEqual(rule["dpi"], 400)

    def test_profile_without_override_inherits_global(self):
        cfg = _cfg(precision_drag_enabled=True, precision_drag_modifier="option",
                   precision_drag_dpi=600)
        self.assertEqual(get_profile_precision_drag(cfg, "figma")["mode"], "inherit")
        self.assertEqual(
            get_precision_drag_for_profile(cfg, "figma"),
            {"enabled": True, "modifier": "option", "dpi": 600},
        )

    def test_profile_off_disables_only_that_app(self):
        cfg = _cfg(precision_drag_enabled=True)
        set_profile_precision_drag(cfg, "figma", "off")
        self.assertFalse(get_precision_drag_for_profile(cfg, "figma")["enabled"])
        self.assertTrue(get_precision_drag_for_profile(cfg, "default")["enabled"])

    def test_profile_custom_uses_its_own_modifier_and_dpi(self):
        cfg = _cfg(precision_drag_enabled=True)
        set_profile_precision_drag(cfg, "figma", "custom", modifier="control", dpi=250)
        self.assertEqual(
            get_precision_drag_for_profile(cfg, "figma"),
            {"enabled": True, "modifier": "control", "dpi": 250},
        )
        # Partial updates keep the other field.
        set_profile_precision_drag(cfg, "figma", "custom", dpi=300)
        self.assertEqual(
            cfg["profiles"]["figma"]["precision_drag"],
            {"mode": "custom", "modifier": "control", "dpi": 300},
        )

    def test_custom_follows_the_global_switch(self):
        cfg = _cfg(precision_drag_enabled=False)
        set_profile_precision_drag(cfg, "figma", "custom", modifier="shift", dpi=200)
        self.assertFalse(get_precision_drag_for_profile(cfg, "figma")["enabled"])

    def test_inherit_removes_the_override_and_unknown_profiles_are_ignored(self):
        cfg = _cfg()
        set_profile_precision_drag(cfg, "figma", "custom", dpi=200)
        set_profile_precision_drag(cfg, "figma", "inherit")
        self.assertNotIn("precision_drag", cfg["profiles"]["figma"])
        set_profile_precision_drag(cfg, "nope", "off")
        self.assertNotIn("nope", cfg["profiles"])


class PrecisionDragHookStateTests(unittest.TestCase):
    """The base hook turns button/modifier state into exactly one shift and
    one restore per precision drag."""

    def _hook(self, enabled=True, dpi=400, base=3600):
        hook = BaseMouseHook()
        hook._connected_device = object()
        hook._hid_gesture = Mock()
        hook._hid_gesture.shift_dpi.return_value = True
        hook.configure_precision_drag(enabled, "command", dpi, lambda: base)
        return hook

    def test_modifier_drag_shifts_and_release_restores(self):
        hook = self._hook()
        hook.precision_button(True, modifier_held=True)
        hook._hid_gesture.shift_dpi.assert_called_once_with(400)
        self.assertTrue(hook.precision_drag_active)
        hook.precision_modifier(True)  # drag events keep reporting it held
        hook.precision_button(False, modifier_held=True)
        hook._hid_gesture.unshift_dpi.assert_called_once_with(3600)
        self.assertFalse(hook.precision_drag_active)
        hook._hid_gesture.shift_dpi.assert_called_once()

    def test_plain_drag_does_nothing(self):
        hook = self._hook()
        hook.precision_button(True, modifier_held=False)
        hook.precision_modifier(False)
        hook.precision_button(False, modifier_held=False)
        hook._hid_gesture.shift_dpi.assert_not_called()
        hook._hid_gesture.unshift_dpi.assert_not_called()

    def test_modifier_pressed_or_released_mid_drag(self):
        hook = self._hook()
        hook.precision_button(True, modifier_held=False)
        hook.precision_modifier(True)
        hook._hid_gesture.shift_dpi.assert_called_once_with(400)
        hook.precision_modifier(False)
        hook._hid_gesture.unshift_dpi.assert_called_once_with(3600)
        hook.precision_button(False, modifier_held=False)
        hook._hid_gesture.unshift_dpi.assert_called_once()

    def test_disabled_rule_never_shifts(self):
        hook = self._hook(enabled=False)
        hook.precision_button(True, modifier_held=True)
        hook._hid_gesture.shift_dpi.assert_not_called()

    def test_disabling_mid_drag_restores(self):
        hook = self._hook()
        hook.precision_button(True, modifier_held=True)
        hook.configure_precision_drag(False, "command", 400)
        hook._hid_gesture.unshift_dpi.assert_called_once_with(3600)

    def test_failed_shift_is_not_remembered_as_active(self):
        hook = self._hook()
        hook._hid_gesture.shift_dpi.return_value = False
        hook.precision_button(True, modifier_held=True)
        self.assertFalse(hook.precision_drag_active)
        hook.precision_button(False, modifier_held=True)
        hook._hid_gesture.unshift_dpi.assert_not_called()

    def test_no_device_or_listener_is_harmless(self):
        hook = self._hook()
        hook._connected_device = None
        hook.precision_button(True, modifier_held=True)
        hook._hid_gesture.shift_dpi.assert_not_called()
        hook._hid_gesture = None
        hook.precision_button(False, modifier_held=True)  # must not raise

    def test_disconnect_clears_active_flag(self):
        hook = self._hook()
        hook.precision_button(True, modifier_held=True)
        hook._on_hid_disconnect()
        self.assertFalse(hook.precision_drag_active)

    def test_disable_mid_drag_then_enable_does_not_shift_without_a_new_press(self):
        hook = self._hook()
        hook.precision_button(True, modifier_held=True)
        hook.configure_precision_drag(False, "command", 400)
        hook._hid_gesture.unshift_dpi.assert_called_once_with(3600)
        # The platform keeps feeding button state while the rule is off.
        hook.precision_button(False, modifier_held=True)
        hook.configure_precision_drag(True, "command", 400)
        hook.precision_modifier(True)  # a flags event, button long released
        hook._hid_gesture.shift_dpi.assert_called_once()  # only the first one
        hook.precision_button(True, modifier_held=True)
        self.assertEqual(hook._hid_gesture.shift_dpi.call_count, 2)

    def test_new_dpi_mid_drag_reshifts_with_the_new_value(self):
        hook = self._hook()
        hook.precision_button(True, modifier_held=True)
        hook.configure_precision_drag(True, "command", 300)
        hook._hid_gesture.unshift_dpi.assert_called_once_with(3600)
        hook._hid_gesture.shift_dpi.assert_called_with(300)

    def test_new_modifier_mid_drag_waits_for_a_fresh_sample(self):
        hook = self._hook()
        hook.precision_button(True, modifier_held=True)
        hook.configure_precision_drag(True, "option", 400)
        hook._hid_gesture.unshift_dpi.assert_called_once_with(3600)
        self.assertFalse(hook.precision_drag_active)
        hook._hid_gesture.shift_dpi.assert_called_once()
        hook.precision_modifier(True)  # next event reports Option held
        self.assertEqual(hook._hid_gesture.shift_dpi.call_count, 2)

    def test_end_precision_drag_restores_and_forgets_state(self):
        hook = self._hook()
        hook.precision_button(True, modifier_held=True)
        hook._end_precision_drag()
        hook._hid_gesture.unshift_dpi.assert_called_once_with(3600)
        self.assertFalse(hook._precision_button_down)
        self.assertFalse(hook._precision_modifier_held)
        hook._hid_gesture.reset_mock()
        hook._end_precision_drag()  # idempotent
        hook._hid_gesture.unshift_dpi.assert_not_called()

    def test_restore_uses_the_current_base_dpi(self):
        base = {"dpi": 3600}
        hook = BaseMouseHook()
        hook._connected_device = object()
        hook._hid_gesture = Mock()
        hook._hid_gesture.shift_dpi.return_value = True
        hook.configure_precision_drag(True, "command", 400, lambda: base["dpi"])
        hook.precision_button(True, modifier_held=True)
        base["dpi"] = 1600  # user changed the pointer speed mid-drag
        hook.precision_button(False, modifier_held=True)
        hook._hid_gesture.unshift_dpi.assert_called_once_with(1600)


class PrecisionDragHidWriteTests(unittest.TestCase):
    """shift/unshift write setSensorDpi immediately from the caller's thread
    and keep the power-cycle verify from undoing a shift mid-drag."""

    def _listener(self):
        listener = hid_gesture.HidGestureListener()
        listener._dev = object()
        listener._dpi_idx = 0x0D
        listener._last_dpi_set = 3600
        return listener

    def test_shift_writes_setsensordpi_and_marks_shift(self):
        listener = self._listener()
        with patch.object(listener, "_tx") as tx:
            self.assertTrue(listener.shift_dpi(400))
        tx.assert_called_once_with(hid_gesture.LONG_ID, 0x0D, 3, [0x00, 0x01, 0x90],
                                   sw=hid_gesture.SHIFT_SW)
        self.assertEqual(listener._dpi_shift_active, 400)
        self.assertEqual(listener._last_dpi_set, 3600)

    def test_unshift_restores_and_arms_a_followup_verify(self):
        listener = self._listener()
        listener._dpi_shift_active = 400
        listener._followup_verify_at = [1000.0]
        with (
            patch.object(listener, "_tx") as tx,
            patch.object(hid_gesture.time, "monotonic", return_value=100.0),
        ):
            self.assertTrue(listener.unshift_dpi(3600))
        tx.assert_called_once_with(hid_gesture.LONG_ID, 0x0D, 3, [0x00, 0x0E, 0x10],
                                   sw=hid_gesture.SHIFT_SW)
        self.assertIsNone(listener._dpi_shift_active)
        self.assertEqual(
            listener._followup_verify_at,
            [100.0 + listener._UNSHIFT_VERIFY_DELAY_S, 1000.0],
        )

    def test_shift_uses_its_own_software_id(self):
        listener = self._listener()
        with patch.object(listener, "_dev") as dev:
            listener._dev_idx = 1
            listener.shift_dpi(400)
        buf = dev.write.call_args[0][0]
        self.assertEqual(buf[:7], [hid_gesture.LONG_ID, 1, 0x0D, (3 << 4) | hid_gesture.SHIFT_SW,
                                   0x00, 0x01, 0x90])
        self.assertNotEqual(hid_gesture.SHIFT_SW, hid_gesture.MY_SW)

    def test_request_ignores_replies_and_errors_of_precision_writes(self):
        listener = self._listener()
        listener._dev_idx = 1
        err = [0x11, 1, 0xFF, 0x0D, (3 << 4) | hid_gesture.SHIFT_SW, 0x02] + [0] * 14
        ack = [0x11, 1, 0x0D, (3 << 4) | hid_gesture.SHIFT_SW, 0x00, 0x01, 0x90] + [0] * 13
        reply = [0x11, 1, 0x0D, (2 << 4) | hid_gesture.MY_SW, 0x00, 0x0E, 0x10] + [0] * 13
        with (
            patch.object(listener, "_tx"),
            patch.object(listener, "_rx", side_effect=[err, ack, reply]),
            patch.object(listener, "_on_report") as on_report,
            patch("builtins.print"),
        ):
            msg = listener._request(0x0D, 2, [0x00])
        self.assertIsNotNone(msg)
        self.assertEqual(msg[4][:3], [0x00, 0x0E, 0x10])
        on_report.assert_called_once_with(ack)

    def test_verify_ignores_a_shift_that_started_during_the_read(self):
        listener = self._listener()

        def _read(*_args, **_kwargs):
            listener._dpi_shift_active = 400  # drag began while we waited
            return (1, 0x0D, 2, hid_gesture.MY_SW, [0x00, 0x01, 0x90])

        with patch.object(listener, "_request", side_effect=_read), patch("builtins.print"):
            listener._verify_device_state()
        self.assertIsNone(listener._pending_dpi)

    def test_write_failure_or_no_device_reports_false(self):
        listener = self._listener()
        with patch.object(listener, "_tx", side_effect=OSError("gone")), patch("builtins.print"):
            self.assertFalse(listener.shift_dpi(400))
        self.assertIsNone(listener._dpi_shift_active)
        listener._dev = None
        self.assertFalse(listener.shift_dpi(400))

    def test_verify_skips_while_shifted(self):
        listener = self._listener()
        listener._dpi_shift_active = 400
        with patch.object(listener, "_request") as request:
            listener._verify_device_state()
        request.assert_not_called()
        self.assertIsNone(listener._pending_dpi)

    def test_verify_after_unshift_corrects_a_lost_restore(self):
        listener = self._listener()
        listener._dpi_shift_active = 400
        with patch.object(listener, "_tx"):
            listener.unshift_dpi(3600)
        reply = (1, 0x0D, 2, hid_gesture.MY_SW, [0x00, 0x01, 0x90])  # still 400
        with (
            patch.object(listener, "_request", return_value=reply),
            patch.object(listener, "_divert"),
            patch.object(listener, "_divert_extras"),
            patch("builtins.print"),
        ):
            listener._verify_device_state()
        self.assertEqual(listener._pending_dpi, 3600)

    def test_full_dpi_write_ends_the_shift(self):
        listener = self._listener()
        listener._dpi_shift_active = 400
        listener._pending_dpi = 1600
        reply = (1, 0x0D, 3, hid_gesture.MY_SW, [0x00, 0x06, 0x40])
        with patch.object(listener, "_request", return_value=reply), patch("builtins.print"):
            listener._apply_pending_dpi()
        self.assertIsNone(listener._dpi_shift_active)
        self.assertEqual(listener._last_dpi_set, 1600)


class PrecisionDragEngineTests(unittest.TestCase):
    def _engine(self, cfg):
        from core.engine import Engine
        from tests.test_engine import _FakeAppDetector, _FakeMouseHook

        class _Hook(_FakeMouseHook):
            def __init__(self):
                super().__init__()
                self.precision_calls = []

            def configure_precision_drag(self, enabled, modifier, dpi, base_dpi_provider=None):
                self.precision_calls.append((enabled, modifier, dpi, base_dpi_provider))

        with (
            patch("core.engine.MouseHook", _Hook),
            patch("core.engine.AppDetector", _FakeAppDetector),
            patch("core.engine.load_config", return_value=cfg),
        ):
            return Engine()

    def test_setup_pushes_the_active_profile_rule_and_base_dpi(self):
        cfg = _cfg(precision_drag_enabled=True, precision_drag_dpi=400, dpi=3600)
        set_profile_precision_drag(cfg, "figma", "custom", modifier="option", dpi=250)
        engine = self._engine(cfg)
        enabled, modifier, dpi, provider = engine.hook.precision_calls[-1]
        self.assertEqual((enabled, modifier, dpi), (True, PRECISION_DRAG_DEFAULT_MODIFIER, 400))
        self.assertEqual(provider(), 3600)

        engine._switch_profile("figma")
        enabled, modifier, dpi, _ = engine.hook.precision_calls[-1]
        self.assertEqual((enabled, modifier, dpi), (True, "option", 250))

    def test_precision_dpi_is_clamped_to_the_device_range(self):
        cfg = _cfg(precision_drag_enabled=True, precision_drag_dpi=50)
        engine = self._engine(cfg)
        _, _, dpi, _ = engine.hook.precision_calls[-1]
        self.assertGreaterEqual(dpi, 200)


class PrecisionDragMacTapTests(unittest.TestCase):
    """The listen-only CGEventTap feeds primary-button state and the
    modifier bit of each event into the base state machine."""

    def setUp(self):
        # Always load a private copy of the module against a fake Quartz:
        # other suites leave the shared module with Quartz stripped.
        self._module_name = "core.mouse_hook_macos"
        self._saved_module = sys.modules.pop(self._module_name, None)
        self._saved_attr = getattr(core, "mouse_hook_macos", None)
        fake_objc = SimpleNamespace(autorelease_pool=MagicMock())
        fake_quartz = MagicMock(name="Quartz")
        with patch.dict(sys.modules, {"objc": fake_objc, "Quartz": fake_quartz}):
            self.module = importlib.import_module(self._module_name)
        self.addCleanup(self._unload)
        self.quartz = self.module.Quartz
        self.hook = self.module.MouseHook()
        self.hook._connected_device = object()
        self.hook._hid_gesture = Mock()
        self.hook._hid_gesture.shift_dpi.return_value = True
        self.hook.configure_precision_drag(True, "option", 300, lambda: 3600)

    def _unload(self):
        sys.modules.pop(self._module_name, None)
        if self._saved_module is not None:
            sys.modules[self._module_name] = self._saved_module
        if self._saved_attr is not None:
            core.mouse_hook_macos = self._saved_attr
        elif getattr(core, "mouse_hook_macos", None) is self.module:
            delattr(core, "mouse_hook_macos")

    def _send(self, event_type, flags):
        with patch.object(self.quartz, "CGEventGetFlags", return_value=flags):
            return self.hook._precision_tap_callback(None, event_type, "evt", None)

    def test_option_drag_shifts_and_release_restores(self):
        option = self.module._PRECISION_FLAG_MASKS["option"]
        self.assertIs(self._send(self.quartz.kCGEventLeftMouseDown, option), "evt")
        self.hook._hid_gesture.shift_dpi.assert_called_once_with(300)
        self._send(self.quartz.kCGEventLeftMouseDragged, option)
        self._send(self.quartz.kCGEventLeftMouseUp, option)
        self.hook._hid_gesture.unshift_dpi.assert_called_once_with(3600)

    def test_other_modifier_does_not_count(self):
        command = self.module._PRECISION_FLAG_MASKS["command"]
        self._send(self.quartz.kCGEventLeftMouseDown, command)
        self.hook._hid_gesture.shift_dpi.assert_not_called()

    def test_modifier_pressed_during_drag_shifts(self):
        option = self.module._PRECISION_FLAG_MASKS["option"]
        self._send(self.quartz.kCGEventLeftMouseDown, 0)
        self._send(self.quartz.kCGEventLeftMouseDragged, 0)
        self._send(self.quartz.kCGEventLeftMouseDragged, option)
        self.hook._hid_gesture.shift_dpi.assert_called_once_with(300)

    def test_modifier_released_without_motion_restores(self):
        option = self.module._PRECISION_FLAG_MASKS["option"]
        self._send(self.quartz.kCGEventLeftMouseDown, option)
        self._send(self.quartz.kCGEventFlagsChanged, 0)
        self.hook._hid_gesture.unshift_dpi.assert_called_once_with(3600)
        self._send(self.quartz.kCGEventFlagsChanged, option)
        self.assertEqual(self.hook._hid_gesture.shift_dpi.call_count, 2)

    def test_tap_disabled_mid_drag_restores_and_rearms(self):
        option = self.module._PRECISION_FLAG_MASKS["option"]
        self._send(self.quartz.kCGEventLeftMouseDown, option)
        self.hook._precision_tap = object()
        with patch.object(self.quartz, "CGEventTapEnable") as enable, patch("builtins.print"):
            self.hook._precision_tap_callback(
                None, self.module._kCGEventTapDisabledByTimeout, "evt", None
            )
        self.hook._hid_gesture.unshift_dpi.assert_called_once_with(3600)
        enable.assert_called_once_with(self.hook._precision_tap, True)
        self.assertFalse(self.hook._precision_button_down)

    def test_stop_restores_dpi_before_the_hid_listener_goes_away(self):
        option = self.module._PRECISION_FLAG_MASKS["option"]
        self._send(self.quartz.kCGEventLeftMouseDown, option)
        order = []
        with (
            patch.object(self.hook, "_stop_device_monitor"),
            patch.object(self.hook, "_unregister_wake_observer"),
            patch.object(self.hook, "_stop_hid_listener",
                         side_effect=lambda: order.append("hid")),
            patch.object(self.hook._hid_gesture, "unshift_dpi",
                         side_effect=lambda _dpi: order.append("unshift")),
            patch("builtins.print"),
        ):
            self.hook.stop()
        self.assertEqual(order, ["unshift", "hid"])

    def test_disabled_rule_or_no_device_passes_through(self):
        option = self.module._PRECISION_FLAG_MASKS["option"]
        self.hook.configure_precision_drag(False, "option", 300)
        self._send(self.quartz.kCGEventLeftMouseDown, option)
        self._send(self.quartz.kCGEventLeftMouseUp, option)
        self.hook.configure_precision_drag(True, "option", 300)
        self.hook._connected_device = None
        self._send(self.quartz.kCGEventLeftMouseDown, option)
        self.hook._hid_gesture.shift_dpi.assert_not_called()


if __name__ == "__main__":
    unittest.main()
