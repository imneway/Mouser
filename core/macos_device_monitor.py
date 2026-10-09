"""
macOS device arrival/removal monitor for Logitech HID interfaces.

Windows feeds ``HidGestureListener.notify_device_change()`` from
``WM_DEVICECHANGE`` so a freshly plugged receiver is probed right away instead
of after the reconnect backoff (up to 30 s). This module is the macOS
counterpart: it watches IOKit for ``IOHIDDevice`` services with the Logitech
vendor ID appearing or disappearing and invokes a callback.

It uses ``IOServiceAddMatchingNotification`` on a dedicated thread's run loop.
That only observes the IORegistry -- it never opens a device or creates an
``IOHIDManager``, so it cannot contend with Mouser's own HID++ access or
reintroduce the manager leak from issue #238. The port, run-loop source and
both iterators are created exactly once per ``start()`` and released in
``stop()``.
"""

import contextlib
import sys
import threading

LOGITECH_VENDOR_ID = 0x046D

_K_IO_FIRST_MATCH_NOTIFICATION = b"IOServiceFirstMatch"
_K_IO_TERMINATED_NOTIFICATION = b"IOServiceTerminate"
_K_CF_NUMBER_SINT32 = 3
_K_CF_STRING_ENCODING_UTF8 = 0x08000100
_RUN_LOOP_SLICE_S = 0.5


def _load_native():
    """Bind the CoreFoundation/IOKit entry points; ``None`` off macOS."""
    if sys.platform != "darwin":
        return None
    import ctypes
    from ctypes import POINTER, c_bool, c_char_p, c_double, c_int, c_uint32, c_void_p

    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")

    cf.CFNumberCreate.argtypes = [c_void_p, c_int, c_void_p]
    cf.CFNumberCreate.restype = c_void_p
    cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_int]
    cf.CFStringCreateWithCString.restype = c_void_p
    cf.CFDictionarySetValue.argtypes = [c_void_p, c_void_p, c_void_p]
    cf.CFDictionarySetValue.restype = None
    cf.CFRetain.argtypes = [c_void_p]
    cf.CFRetain.restype = c_void_p
    cf.CFRelease.argtypes = [c_void_p]
    cf.CFRelease.restype = None
    cf.CFRunLoopGetCurrent.argtypes = []
    cf.CFRunLoopGetCurrent.restype = c_void_p
    cf.CFRunLoopAddSource.argtypes = [c_void_p, c_void_p, c_void_p]
    cf.CFRunLoopAddSource.restype = None
    cf.CFRunLoopRemoveSource.argtypes = [c_void_p, c_void_p, c_void_p]
    cf.CFRunLoopRemoveSource.restype = None
    cf.CFRunLoopRunInMode.argtypes = [c_void_p, c_double, c_bool]
    cf.CFRunLoopRunInMode.restype = c_int

    iokit.IONotificationPortCreate.argtypes = [c_uint32]
    iokit.IONotificationPortCreate.restype = c_void_p
    iokit.IONotificationPortGetRunLoopSource.argtypes = [c_void_p]
    iokit.IONotificationPortGetRunLoopSource.restype = c_void_p
    iokit.IONotificationPortDestroy.argtypes = [c_void_p]
    iokit.IONotificationPortDestroy.restype = None
    iokit.IOServiceMatching.argtypes = [c_char_p]
    iokit.IOServiceMatching.restype = c_void_p
    callback_type = ctypes.CFUNCTYPE(None, c_void_p, c_uint32)
    iokit.IOServiceAddMatchingNotification.argtypes = [
        c_void_p, c_char_p, c_void_p, callback_type, c_void_p, POINTER(c_uint32),
    ]
    iokit.IOServiceAddMatchingNotification.restype = c_int
    iokit.IOIteratorNext.argtypes = [c_uint32]
    iokit.IOIteratorNext.restype = c_uint32
    iokit.IOObjectRelease.argtypes = [c_uint32]
    iokit.IOObjectRelease.restype = c_int

    default_mode = c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
    return {
        "ctypes": ctypes,
        "cf": cf,
        "iokit": iokit,
        "callback_type": callback_type,
        "default_mode": default_mode,
    }


def _autorelease_pool():
    try:
        import objc

        return objc.autorelease_pool()
    except Exception:
        return contextlib.nullcontext()


class LogitechDeviceMonitor:
    """Calls ``on_change(arrived: bool)`` when a Logitech HID interface
    appears (``True``) or disappears (``False``).

    The callback runs on the monitor thread; keep it short and non-blocking.
    One physical receiver exposes several HID interfaces, so a single plug or
    unplug usually produces a short burst of calls.
    """

    def __init__(self, on_change, vendor_id=LOGITECH_VENDOR_ID):
        self._on_change = on_change
        self._vendor_id = vendor_id
        self._thread = None
        self._stop_event = threading.Event()
        self._ready = threading.Event()
        self._started_ok = False

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout=2.0):
        """Start watching. Returns True once notifications are armed."""
        if self.running:
            return True
        self._stop_event.clear()
        self._ready.clear()
        self._started_ok = False
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="MouseHook-devmon"
        )
        self._thread.start()
        self._ready.wait(timeout)
        return self._started_ok

    def stop(self, timeout=2.0):
        thread, self._thread = self._thread, None
        self._stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    # -- monitor thread -----------------------------------------------------

    def _run(self):
        try:
            native = _load_native()
        except Exception as exc:
            print(f"[DeviceMonitor] IOKit unavailable: {exc}")
            native = None
        if native is None:
            self._ready.set()
            return

        ctypes = native["ctypes"]
        cf = native["cf"]
        iokit = native["iokit"]
        iterators = []
        matching = None
        port = None
        source = None
        run_loop = None

        def _drain(iterator):
            count = 0
            while True:
                service = iokit.IOIteratorNext(iterator)
                if not service:
                    return count
                iokit.IOObjectRelease(service)
                count += 1

        def _make_handler(arrived):
            def _handler(_refcon, iterator):
                # The iterator must be drained to re-arm the notification.
                if _drain(iterator) and not self._stop_event.is_set():
                    try:
                        self._on_change(arrived)
                    except Exception as exc:
                        print(f"[DeviceMonitor] change callback failed: {exc}")
            return native["callback_type"](_handler)

        # Keep the ctypes trampolines alive for as long as IOKit may call them.
        callbacks = (_make_handler(True), _make_handler(False))

        try:
            with _autorelease_pool():
                port = iokit.IONotificationPortCreate(0)
                if not port:
                    raise OSError("IONotificationPortCreate failed")
                source = iokit.IONotificationPortGetRunLoopSource(port)
                run_loop = cf.CFRunLoopGetCurrent()
                cf.CFRunLoopAddSource(run_loop, source, native["default_mode"])

                matching = iokit.IOServiceMatching(b"IOHIDDevice")
                if not matching:
                    raise OSError("IOServiceMatching(IOHIDDevice) failed")
                key = cf.CFStringCreateWithCString(
                    None, b"VendorID", _K_CF_STRING_ENCODING_UTF8
                )
                vid = ctypes.c_int32(self._vendor_id)
                value = cf.CFNumberCreate(None, _K_CF_NUMBER_SINT32, ctypes.byref(vid))
                cf.CFDictionarySetValue(matching, key, value)
                cf.CFRelease(key)
                cf.CFRelease(value)

                for kind, callback in (
                    (_K_IO_FIRST_MATCH_NOTIFICATION, callbacks[0]),
                    (_K_IO_TERMINATED_NOTIFICATION, callbacks[1]),
                ):
                    # Each IOServiceAddMatchingNotification call consumes one
                    # reference to the matching dictionary, success or not;
                    # our own reference is released in ``finally``.
                    cf.CFRetain(matching)
                    iterator = ctypes.c_uint32(0)
                    kr = iokit.IOServiceAddMatchingNotification(
                        port, kind, matching, callback, None, ctypes.byref(iterator)
                    )
                    if kr != 0:
                        raise OSError(
                            f"IOServiceAddMatchingNotification({kind.decode()}) "
                            f"failed: 0x{kr & 0xFFFFFFFF:08X}"
                        )
                    iterators.append(iterator.value)
                    # Arm the notification; devices already present are not
                    # a change, so do not report them.
                    _drain(iterator.value)

            self._started_ok = True
            print("[DeviceMonitor] Watching for Logitech HID arrivals/removals")
            self._ready.set()

            while not self._stop_event.is_set():
                with _autorelease_pool():
                    cf.CFRunLoopRunInMode(
                        native["default_mode"], _RUN_LOOP_SLICE_S, False
                    )
        except Exception as exc:
            print(f"[DeviceMonitor] failed to start: {exc}")
        finally:
            self._ready.set()
            if matching:
                cf.CFRelease(matching)
            for iterator in iterators:
                iokit.IOObjectRelease(iterator)
            if source and run_loop:
                cf.CFRunLoopRemoveSource(run_loop, source, native["default_mode"])
            if port:
                # Also releases the run-loop source owned by the port.
                iokit.IONotificationPortDestroy(port)
            del callbacks
