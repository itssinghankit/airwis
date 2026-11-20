#!/usr/bin/env python3
"""
Menu bar switch for Airwis, the hand gesture mouse.

Shows a hand in the menu bar. Tracking starts as soon as the app opens.
Click the hand to stop / start tracking, or to quit.
Opening the app again while it runs (double-click, Spotlight) starts tracking / shows the preview.
The tracker itself runs as a separate process (gesture_control.py), so stopping it is instant.
"""

import ctypes
import signal
import subprocess
import sys
from pathlib import Path

import objc
import rumps
import rumps.rumps
from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
from Foundation import NSDictionary

HERE = Path(__file__).resolve().parent
TRACKER = HERE / "gesture_control.py"
LOG = Path.home() / "Library/Logs/Airwis.log"  # tracker output, for when something goes wrong
ICON_ON = "🖐"
ICON_OFF = "✋🏻"   # lighter hand = off
ACCESSIBILITY_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"

_ax = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
_ax.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
_ax.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool


def mouse_allowed(ask=False):
    """True if macOS lets this app move the mouse. ask=True adds the app to the
    Accessibility list and shows the system prompt."""
    options = NSDictionary.dictionaryWithDictionary_({"AXTrustedCheckOptionPrompt": ask})
    return _ax.AXIsProcessTrustedWithOptions(objc.pyobjc_id(options))


class GestureMenuBar(rumps.App):
    def __init__(self):
        super().__init__("Airwis", title=ICON_OFF, quit_button=None)
        self.proc = None
        self.toggle_item = rumps.MenuItem("Start tracking", callback=self.toggle)
        self.permission_item = rumps.MenuItem(
            "⚠️ Allow mouse control (Accessibility)…", callback=self.open_permission)
        self.menu = [self.toggle_item, self.permission_item, rumps.MenuItem("Open log", callback=self.open_log),
                     None, rumps.MenuItem("Quit", callback=self.quit)]
        self.allowed = mouse_allowed(ask=True)
        self.start()

    def reopened(self):
        if self.running():
            preview = NSRunningApplication.runningApplicationWithProcessIdentifier_(self.proc.pid)
            if preview:
                preview.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        else:
            self.start()

    def open_permission(self, _):
        subprocess.run(["open", ACCESSIBILITY_SETTINGS])

    def open_log(self, _):
        subprocess.run(["open", "-e", str(LOG)])

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.running():
            return
        LOG.parent.mkdir(parents=True, exist_ok=True)
        log = open(LOG, "w")
        self.proc = subprocess.Popen([sys.executable, "-u", str(TRACKER)], cwd=HERE,
                                     stdout=log, stderr=subprocess.STDOUT)
        self.refresh()

    def stop(self):
        if not self.running():
            return
        self.proc.send_signal(signal.SIGINT)  # lets the tracker let go of the mouse button first
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.refresh()

    def refresh(self):
        on = self.running()
        self.title = ICON_ON if on else ICON_OFF
        self.toggle_item.title = "Stop tracking" if on else "Start tracking"

    def toggle(self, _):
        self.stop() if self.running() else self.start()

    @rumps.timer(1)
    def watch(self, _):
        # Notices when the tracker was closed with Q in its preview window.
        self.refresh()
        allowed = mouse_allowed()
        self.permission_item.set_callback(None if allowed else self.open_permission)
        self.permission_item.title = ("✓ Mouse control allowed" if allowed
                                      else "⚠️ Allow mouse control (Accessibility)…")
        if allowed and not self.allowed and self.running():
            # Permission was just granted: restart the tracker so it can move the mouse.
            self.stop()
            self.start()
        self.allowed = allowed

    def quit(self, _):
        self.stop()
        rumps.quit_application()


class NSApp(objc.Category(rumps.rumps.NSApp)):  # adds a method to rumps' own app delegate
    """macOS calls this when the app is opened again while it is already running."""

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_windows):
        getattr(rumps.App, "*app_instance").reopened()
        return True


if __name__ == "__main__":
    GestureMenuBar().run()
