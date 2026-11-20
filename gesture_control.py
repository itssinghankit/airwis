#!/usr/bin/env python3
"""
Airwis: hand gesture mouse for macOS.

Control the mouse with one hand in front of the built-in webcam.

    Move your hand .................. cursor follows the point midway between thumb tip and index tip
    Quick pinch (thumb + index) ..... left click (pinch twice quickly = double click)
    Pinch and hold .................. click and drag, open fingers to drop
    Pinch thumb + middle finger ..... right click
    Pinch thumb + ring finger ....... double click
    Index + middle up, move up/down . scroll
    Hold a closed fist (~0.6 s) ..... pause / resume tracking
    Q or Esc in the preview window .. quit (Ctrl+C in the terminal also works)

Needs two macOS permissions for the app you run this from (Terminal, iTerm, VS Code...):
Camera, and Accessibility (so it may move the mouse). See README.md.
"""

# =============================================================================
# CONFIG: tweak these to taste
# =============================================================================

# --- Camera ---
CAMERA_INDEX = 0            # try 1 if your iPhone (Continuity Camera) takes over
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
MIRROR = True               # flip the image so moving right moves the cursor right
CAMERA_TIMEOUT_SEC = 30     # quit if the camera sends nothing for this long

# --- Active area (sensitivity) ---
# Only this box in the middle of the camera frame maps to the whole screen,
# so you don't have to stretch your arm to reach the corners.
# Smaller box = more sensitive (less hand movement per screen width).
ACTIVE_AREA_WIDTH = 0.55    # fraction of the frame width
ACTIVE_AREA_HEIGHT = 0.55   # fraction of the frame height
ACTIVE_AREA_CENTER_X = 0.50
ACTIVE_AREA_CENTER_Y = 0.42  # a bit above center: hands tend to sit low in a laptop cam

# --- What moves the cursor: "between" (midpoint of thumb tip and index tip), "thumb" or "index" ---
# "between" barely moves when you pinch, because both fingertips close in toward it.
CURSOR_FINGER = "between"

# --- Smoothing (One Euro filter) ---
# MIN_CUTOFF: lower = steadier when your hand is still, but more lag.
# BETA:       higher = less lag when you move fast.
SMOOTHING_MIN_CUTOFF = 1.0
SMOOTHING_BETA = 0.008
CURSOR_DEADZONE_PX = 1.5    # ignore moves smaller than this (kills tiny tremble)

# --- Pinch ---
# Distances are measured relative to your hand size, so they work near or far from the camera.
PINCH_START = 0.30          # fingertips closer than this = pinch begins
PINCH_RELEASE = 0.42        # must open wider than this to end the pinch (stops flicker)
PINCH_CONFIRM_FRAMES = 2    # pinch must be seen this many frames in a row
HOLD_TO_DRAG_SEC = 0.35     # pinch held longer than this becomes a drag
CLICK_COOLDOWN_SEC = 0.20   # minimum time between two clicks
DOUBLE_CLICK_WINDOW_SEC = 0.70
# Aim lock: as your fingertips close in for a pinch, the cursor stops moving, so the
# click lands where you aimed (the thumb moves the cursor, and it moves when you pinch).
AIM_LOCK_START = 0.60       # fingertips closer than this = cursor holds still
AIM_LOCK_END = 0.75         # open wider than this = cursor follows again
DOUBLE_CLICK_RADIUS_PX = 40
# When a pinch starts, your index fingertip moves toward your thumb and drags the
# cursor with it. We click where the cursor was this long before the pinch instead.
PINCH_LOOKBACK_SEC = 0.12
# Right / double click: the thumb's gap to the middle (or ring) tip must be below this
# fraction of its gap to the other fingertips, so a pinch is never ambiguous (the tips sit close).
PINCH_CLEARANCE = 0.8
# Ring pinch is a bit tighter: in the scroll pose the thumb often rests on the folded ring finger.
RING_PINCH_START = 0.25

# --- Finger shapes ---
EXTENDED_RATIO = 1.15       # tip this much farther from wrist than the middle joint = finger "up"

# --- Scroll ---
SCROLL_SPEED = 3000         # pixels scrolled when your hand moves the full camera height
SCROLL_DIRECTION = 1        # set to -1 if scrolling feels backwards
SCROLL_SMOOTHING = 0.5      # 0..1, lower = smoother but laggier
SCROLL_DEADZONE = 0.002     # ignore tiny up/down wobble

# --- Pause ---
FIST_HOLD_SEC = 0.6         # hold a fist this long to pause / resume

# --- Preview window ---
SHOW_PREVIEW = True
PREVIEW_WIDTH = 480
PREVIEW_ON_TOP = True

# --- Hand tracker ---
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/latest/hand_landmarker.task")
MIN_DETECTION_CONFIDENCE = 0.6
MIN_TRACKING_CONFIDENCE = 0.5

# =============================================================================

import ctypes
import math
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

import cv2
import mediapipe as mp
import Quartz
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import (
    HandLandmarker,
    HandLandmarkerOptions,
    RunningMode,
)

# MediaPipe hand landmark ids
WRIST, THUMB_TIP = 0, 4
INDEX_PIP, INDEX_TIP = 6, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_TIP = 9, 10, 12
RING_PIP, RING_TIP = 14, 16
PINKY_PIP, PINKY_TIP = 18, 20
FINGERS = {
    "index": (INDEX_TIP, INDEX_PIP),
    "middle": (MIDDLE_TIP, MIDDLE_PIP),
    "ring": (RING_TIP, RING_PIP),
    "pinky": (PINKY_TIP, PINKY_PIP),
}
HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
]


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


# -----------------------------------------------------------------------------
# Smoothing
# -----------------------------------------------------------------------------

class OneEuroFilter:
    """Smooths a noisy signal: heavy smoothing when slow, light smoothing when fast."""

    def __init__(self, min_cutoff, beta, d_cutoff=1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.reset()

    def reset(self):
        self.x_prev = None
        self.dx_prev = 0.0
        self.t_prev = None

    @staticmethod
    def _alpha(dt, cutoff):
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x, t):
        if self.x_prev is None:
            self.x_prev, self.t_prev = x, t
            return x
        dt = max(t - self.t_prev, 1e-6)
        a_d = self._alpha(dt, self.d_cutoff)
        dx = (x - self.x_prev) / dt
        dx_hat = a_d * dx + (1 - a_d) * self.dx_prev
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(dt, cutoff)
        x_hat = a * x + (1 - a) * self.x_prev
        self.x_prev, self.dx_prev, self.t_prev = x_hat, dx_hat, t
        return x_hat


# -----------------------------------------------------------------------------
# Mouse (Quartz events: real drag events, no artificial delays)
# -----------------------------------------------------------------------------

class Mouse:
    def __init__(self):
        self.left_is_down = False

    @staticmethod
    def screen_size():
        bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return int(bounds.size.width), int(bounds.size.height)

    @staticmethod
    def position():
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return loc.x, loc.y

    @staticmethod
    def _post(event_type, x, y, button, click_state=1):
        event = Quartz.CGEventCreateMouseEvent(None, event_type, (x, y), button)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, click_state)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def move(self, x, y):
        # While the button is down, apps only see a drag if we send "dragged" events.
        kind = Quartz.kCGEventLeftMouseDragged if self.left_is_down else Quartz.kCGEventMouseMoved
        self._post(kind, x, y, Quartz.kCGMouseButtonLeft)

    def left_down(self, x, y):
        self._post(Quartz.kCGEventLeftMouseDown, x, y, Quartz.kCGMouseButtonLeft)
        self.left_is_down = True

    def left_up(self, x, y):
        self._post(Quartz.kCGEventLeftMouseUp, x, y, Quartz.kCGMouseButtonLeft)
        self.left_is_down = False

    def left_click(self, x, y, click_state=1):
        self._post(Quartz.kCGEventLeftMouseDown, x, y, Quartz.kCGMouseButtonLeft, click_state)
        self._post(Quartz.kCGEventLeftMouseUp, x, y, Quartz.kCGMouseButtonLeft, click_state)

    def right_click(self, x, y):
        self._post(Quartz.kCGEventRightMouseDown, x, y, Quartz.kCGMouseButtonRight)
        self._post(Quartz.kCGEventRightMouseUp, x, y, Quartz.kCGMouseButtonRight)

    @staticmethod
    def scroll(pixels):
        event = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitPixel, 1, int(pixels))
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


# -----------------------------------------------------------------------------
# Gestures
# -----------------------------------------------------------------------------

class GestureController:
    """Turns hand landmarks into mouse actions. update() returns the gesture name to show."""

    def __init__(self, mouse):
        self.mouse = mouse
        self.screen_w, self.screen_h = mouse.screen_size()
        self.filter_x = OneEuroFilter(SMOOTHING_MIN_CUTOFF, SMOOTHING_BETA)
        self.filter_y = OneEuroFilter(SMOOTHING_MIN_CUTOFF, SMOOTHING_BETA)
        self.cursor = mouse.position()
        self.history = deque(maxlen=60)  # (time, x, y) of recent cursor positions
        # Shift added to the hand position, so the cursor doesn't jump when a drag starts.
        self.offset = [0.0, 0.0]

        self.paused = False
        self.fist_since = None
        self.fist_handled = False

        self.left_frames = 0
        self.left_active = False
        self.left_start = 0.0
        self.pinch_pos = None
        self.dragging = False
        self.last_click_t = -1.0
        self.last_click_pos = (-1e9, -1e9)
        self.click_state = 0

        self.right_frames = 0
        self.right_held = False
        self.last_right_t = -1.0
        self.double_frames = 0
        self.double_held = False

        self.aim = None        # where the cursor is locked while fingers close in
        self.pinch_gap = None  # shown in the preview, to help tune PINCH_START
        self.gaps = self.reach = None

        self.scroll_y = None
        self.scroll_prev = None
        self.scroll_carry = 0.0

    # --- helpers ---

    def _map_to_screen(self, nx, ny):
        u = (nx - (ACTIVE_AREA_CENTER_X - ACTIVE_AREA_WIDTH / 2)) / ACTIVE_AREA_WIDTH
        v = (ny - (ACTIVE_AREA_CENTER_Y - ACTIVE_AREA_HEIGHT / 2)) / ACTIVE_AREA_HEIGHT
        u = min(max(u, 0.0), 1.0)
        v = min(max(v, 0.0), 1.0)
        return u * (self.screen_w - 1), v * (self.screen_h - 1)

    def _move(self, x, y):
        x = min(max(x, 0), self.screen_w - 1)
        y = min(max(y, 0), self.screen_h - 1)
        if dist((x, y), self.cursor) >= CURSOR_DEADZONE_PX:
            self.mouse.move(x, y)
            self.cursor = (x, y)

    def _cursor_before(self, now):
        """Where the cursor was PINCH_LOOKBACK_SEC ago (before the fingers started closing)."""
        target = now - PINCH_LOOKBACK_SEC
        for t, x, y in reversed(self.history):
            if t <= target:
                return (x, y)
        return self.history[0][1:] if self.history else self.cursor

    def _end_left(self, allow_click, now):
        label = None
        if self.dragging:
            self.mouse.left_up(*self.cursor)
            self.dragging = False
            label = "Drop"
        elif self.left_active and allow_click:
            label = self._click(now)
        self.left_active = False
        self.left_frames = 0
        return label

    def _click(self, now):
        if now - self.last_click_t < CLICK_COOLDOWN_SEC:
            return "Pinch"
        x, y = self.pinch_pos
        repeat = (now - self.last_click_t < DOUBLE_CLICK_WINDOW_SEC
                  and dist((x, y), self.last_click_pos) < DOUBLE_CLICK_RADIUS_PX)
        self.click_state = self.click_state + 1 if repeat else 1
        self.mouse.left_click(x, y, self.click_state)
        print(f"Click x{self.click_state} at ({x:.0f}, {y:.0f})")
        self.last_click_t = now
        self.last_click_pos = (x, y)
        return {1: "Click", 2: "Double click"}.get(self.click_state, "Triple click")

    def _reset_scroll(self):
        self.scroll_y = self.scroll_prev = None
        self.scroll_carry = 0.0

    def release_all(self):
        """Never leave the mouse button stuck down."""
        if self.mouse.left_is_down:
            self.mouse.left_up(*self.cursor)
        self.dragging = False
        self.left_active = False

    def hand_lost(self):
        self.release_all()
        self.left_frames = 0
        self.right_frames = 0
        self.right_held = False
        self.double_frames = 0
        self.double_held = False
        self.aim = None
        self.pinch_gap = None
        self.gaps = None
        self.fist_since = None
        self.fist_handled = False
        self.filter_x.reset()
        self.filter_y.reset()
        self._reset_scroll()

    # --- main step ---

    def update(self, landmarks, frame_w, frame_h, now):
        if landmarks is None:
            self.hand_lost()
            return "PAUSED" if self.paused else "No hand"

        # Pixel coordinates, so distances aren't stretched by the frame's aspect ratio.
        pts = [(p.x * frame_w, p.y * frame_h) for p in landmarks]
        wrist = pts[WRIST]
        hand_size = max(dist(wrist, pts[MIDDLE_MCP]), 1e-6)
        reach = {name: dist(wrist, pts[tip]) / max(dist(wrist, pts[pip]), 1e-6)
                 for name, (tip, pip) in FINGERS.items()}
        up = {name: r > EXTENDED_RATIO for name, r in reach.items()}
        is_fist = all(r < 1.0 for r in reach.values())  # every fingertip tucked into the palm
        index_gap = dist(pts[THUMB_TIP], pts[INDEX_TIP]) / hand_size
        middle_gap = dist(pts[THUMB_TIP], pts[MIDDLE_TIP]) / hand_size
        ring_gap = dist(pts[THUMB_TIP], pts[RING_TIP]) / hand_size
        self.gaps = {"index": index_gap, "middle": middle_gap, "ring": ring_gap}
        self.reach = reach

        # Smoothed cursor target from the chosen fingertip.
        if CURSOR_FINGER == "between":
            tip = ((pts[THUMB_TIP][0] + pts[INDEX_TIP][0]) / 2, (pts[THUMB_TIP][1] + pts[INDEX_TIP][1]) / 2)
        else:
            tip = pts[THUMB_TIP if CURSOR_FINGER == "thumb" else INDEX_TIP]
        tx, ty = self._map_to_screen(tip[0] / frame_w, tip[1] / frame_h)
        sx, sy = self.filter_x(tx, now), self.filter_y(ty, now)
        if not self.dragging:
            self.offset = [o * 0.85 for o in self.offset]  # ease any leftover drag shift away
        self.history.append((now, *self.cursor))

        self.pinch_gap = min(index_gap, middle_gap, ring_gap)
        if self.aim is None and self.pinch_gap < AIM_LOCK_START:
            self.aim = self._cursor_before(now)
        elif self.aim is not None and self.pinch_gap > AIM_LOCK_END and not self.left_active:
            self.aim = None

        # 1) Fist: hold to pause / resume.
        if is_fist:
            self._end_left(allow_click=False, now=now)
            self._reset_scroll()
            if self.fist_since is None:
                self.fist_since = now
            if not self.fist_handled and now - self.fist_since >= FIST_HOLD_SEC:
                self.paused = not self.paused
                self.fist_handled = True
            if self.fist_handled:
                return "PAUSED" if self.paused else "Resumed"
            return "Fist (hold)"
        self.fist_since = None
        self.fist_handled = False
        if self.paused:
            return "PAUSED"

        # 2) Thumb + index pinch: click, or hold to drag.
        if self.left_active:
            left_pinched = index_gap < PINCH_RELEASE
        else:  # thumb must be nearer the index tip than the other fingertips
            left_pinched = index_gap < PINCH_START and index_gap <= min(middle_gap, ring_gap)
        if not self.left_active:
            self.left_frames = self.left_frames + 1 if left_pinched else 0
            if self.left_frames >= PINCH_CONFIRM_FRAMES:
                self.left_active = True
                self.left_start = now
                self.pinch_pos = self.aim or self._cursor_before(now)
                self._move(*self.pinch_pos)
                self._reset_scroll()
                return "Pinch"
        elif left_pinched:
            if not self.dragging and now - self.left_start >= HOLD_TO_DRAG_SEC:
                self.mouse.left_down(*self.pinch_pos)
                self.dragging = True
                self.offset = [self.pinch_pos[0] - sx, self.pinch_pos[1] - sy]
            if self.dragging:
                self._move(sx + self.offset[0], sy + self.offset[1])
                return "Drag"
            return "Pinch"
        else:
            return self._end_left(allow_click=True, now=now) or "Pinch"

        # 3) Thumb + middle pinch (index finger not tucked in): right click.
        index_out = reach["index"] > 1.0
        right_pinched = (middle_gap < PINCH_START
                         and middle_gap < min(index_gap, ring_gap) * PINCH_CLEARANCE
                         and index_out)
        self.right_frames = self.right_frames + 1 if right_pinched else 0
        if self.right_held and middle_gap > PINCH_RELEASE:
            self.right_held = False
        if self.right_frames >= PINCH_CONFIRM_FRAMES and not self.right_held:
            self.right_held = True
            if now - self.last_right_t >= CLICK_COOLDOWN_SEC:
                x, y = self.aim or self._cursor_before(now)
                self.mouse.right_click(x, y)
                print(f"Right click at ({x:.0f}, {y:.0f})")
                self.last_right_t = now
        if self.right_held:
            self._reset_scroll()
            return "Right click"

        # 4) Thumb + ring pinch (index finger not tucked in): double click.
        double_pinched = (ring_gap < RING_PINCH_START
                          and ring_gap < min(index_gap, middle_gap) * PINCH_CLEARANCE
                          and index_out)
        self.double_frames = self.double_frames + 1 if double_pinched else 0
        if self.double_held and ring_gap > PINCH_RELEASE:
            self.double_held = False
        if self.double_frames >= PINCH_CONFIRM_FRAMES and not self.double_held:
            self.double_held = True
            if now - self.last_click_t >= CLICK_COOLDOWN_SEC:
                x, y = self.aim or self._cursor_before(now)
                self.mouse.left_click(x, y, 1)
                self.mouse.left_click(x, y, 2)
                print(f"Double click (ring) at ({x:.0f}, {y:.0f})")
                self.last_click_t = now
                self.click_state = 0  # a following index pinch is a fresh single click
        if self.double_held:
            self._reset_scroll()
            return "Double click"

        # 5) Index + middle up, ring + pinky down: scroll by moving the hand up/down.
        if up["index"] and up["middle"] and not up["ring"] and not up["pinky"]:
            y = (pts[INDEX_TIP][1] + pts[MIDDLE_TIP][1]) / 2 / frame_h
            self.scroll_y = y if self.scroll_y is None else (
                self.scroll_y + SCROLL_SMOOTHING * (y - self.scroll_y))
            if self.scroll_prev is not None:
                dy = self.scroll_y - self.scroll_prev
                if abs(dy) > SCROLL_DEADZONE:
                    # Hand up -> page moves up, like pushing paper on a desk.
                    self.scroll_carry += dy * SCROLL_SPEED * SCROLL_DIRECTION
                    whole = int(self.scroll_carry)
                    if whole:
                        self.mouse.scroll(whole)
                        self.scroll_carry -= whole
            self.scroll_prev = self.scroll_y
            return "Scroll"
        self._reset_scroll()

        # 6) Anything else: move the cursor.
        if self.aim is not None:
            return "Aim (cursor locked)"
        self._move(sx + self.offset[0], sy + self.offset[1])
        return "Move"


# -----------------------------------------------------------------------------
# Setup helpers
# -----------------------------------------------------------------------------

def accessibility_granted():
    try:
        lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return lib.AXIsProcessTrusted()
    except OSError:
        return True  # can't tell; assume yes


def bring_to_front():
    """Show the preview window in front of other apps, not hidden behind them."""
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


def ensure_model():
    path = Path(__file__).resolve().parent / "hand_landmarker.task"
    if not path.exists():
        print("Downloading the hand tracking model (~8 MB, first run only)...")
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URL, tmp)
        tmp.rename(path)
    return path


def open_camera():
    cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_AVFOUNDATION)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    if not cap.isOpened():
        sys.exit(
            f"Could not open camera {CAMERA_INDEX}.\n"
            "  - Allow camera access: System Settings > Privacy & Security > Camera,\n"
            "    turn on your terminal app, then restart it.\n"
            "  - Or change CAMERA_INDEX at the top of this file (try 1).")
    return cap


FLASH_LABELS = ("Click", "Double click", "Triple click", "Right click", "Drop")
COLORS = {  # BGR
    "Move": (80, 220, 80), "Pinch": (0, 200, 255), "Click": (0, 200, 255),
    "Double click": (0, 200, 255), "Triple click": (0, 200, 255),
    "Drag": (0, 140, 255), "Drop": (0, 140, 255), "Right click": (255, 120, 200),
    "Scroll": (255, 200, 0), "PAUSED": (80, 80, 255), "Fist (hold)": (180, 180, 180),
}


FINGER_TIPS = {"index": INDEX_TIP, "middle": MIDDLE_TIP, "ring": RING_TIP}
YELLOW = (0, 220, 255)  # BGR


def draw_preview(frame, landmarks, label, fps, gaps):
    h, w = frame.shape[:2]
    x0 = int((ACTIVE_AREA_CENTER_X - ACTIVE_AREA_WIDTH / 2) * w)
    y0 = int((ACTIVE_AREA_CENTER_Y - ACTIVE_AREA_HEIGHT / 2) * h)
    x1 = int((ACTIVE_AREA_CENTER_X + ACTIVE_AREA_WIDTH / 2) * w)
    y1 = int((ACTIVE_AREA_CENTER_Y + ACTIVE_AREA_HEIGHT / 2) * h)
    cv2.rectangle(frame, (x0, y0), (x1, y1), (255, 255, 255), 1)

    starts = {"index": PINCH_START, "middle": PINCH_START, "ring": RING_PINCH_START}
    if landmarks is not None:
        pts = [(int(p.x * w), int(p.y * h)) for p in landmarks]
        for a, b in HAND_CONNECTIONS:
            cv2.line(frame, pts[a], pts[b], (220, 220, 220), 2)
        big = (THUMB_TIP, INDEX_TIP, MIDDLE_TIP, RING_TIP)
        for i, p in enumerate(pts):
            cv2.circle(frame, p, 7 if i in big else 3, YELLOW if i in big else (0, 0, 255), -1)
        if gaps:
            # Yellow line from the thumb to the fingertip it is closing in on.
            name = min(gaps, key=gaps.get)
            if gaps[name] < AIM_LOCK_START:
                touching = gaps[name] < starts[name]
                cv2.line(frame, pts[THUMB_TIP], pts[FINGER_TIPS[name]], YELLOW, 5 if touching else 2)

    scale = PREVIEW_WIDTH / w
    small = cv2.resize(frame, (PREVIEW_WIDTH, int(h * scale)))
    color = COLORS.get(label, (230, 230, 230))
    cv2.rectangle(small, (0, 0), (PREVIEW_WIDTH, 34), (0, 0, 0), -1)
    cv2.putText(small, label, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.75, color, 2, cv2.LINE_AA)
    cv2.putText(small, f"{fps:4.0f} fps   Q: quit", (PREVIEW_WIDTH - 170, 23),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1, cv2.LINE_AA)
    if gaps:
        # Live thumb-to-fingertip gaps; a finger turns yellow when it counts as touching.
        sh = small.shape[0]
        cv2.rectangle(small, (0, sh - 30), (PREVIEW_WIDTH, sh), (0, 0, 0), -1)
        x = 10
        for name, gap in gaps.items():
            text = f"{name} {gap:.2f}"
            cv2.putText(small, text, (x, sh - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        YELLOW if gap < starts[name] else (230, 230, 230), 1, cv2.LINE_AA)
            x += 150
    return small


# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------

def main():
    if not accessibility_granted():
        print("WARNING: no Accessibility permission, so the mouse won't move.\n"
              "  System Settings > Privacy & Security > Accessibility: turn on\n"
              "  'Airwis' (or your terminal app if you run it from one), then restart it.\n")

    options = HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(ensure_model())),
        running_mode=RunningMode.VIDEO,
        num_hands=1,
        min_hand_detection_confidence=MIN_DETECTION_CONFIDENCE,
        min_hand_presence_confidence=MIN_TRACKING_CONFIDENCE,
        min_tracking_confidence=MIN_TRACKING_CONFIDENCE,
    )
    landmarker = HandLandmarker.create_from_options(options)
    cap = open_camera()
    controller = GestureController(Mouse())
    window = "Airwis"
    if SHOW_PREVIEW:
        cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
        if PREVIEW_ON_TOP:
            try:
                cv2.setWindowProperty(window, cv2.WND_PROP_TOPMOST, 1)
            except cv2.error:
                pass

    print("Running. Hold a fist to pause/resume. Press Q in the preview window (or Ctrl+C) to quit.")
    last_ts = 0
    fps, prev_t = 0.0, time.monotonic()
    last_frame_t = time.monotonic()
    shown = False
    flash_label, flash_until, last_debug_t = None, 0.0, 0.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                # The camera sends nothing while macOS shows the "allow camera?" prompt,
                # so give it time instead of giving up.
                if time.monotonic() - last_frame_t > CAMERA_TIMEOUT_SEC:
                    sys.exit("No frames from the camera. Check System Settings > "
                             "Privacy & Security > Camera, or try another CAMERA_INDEX.")
                time.sleep(0.05)
                continue
            last_frame_t = time.monotonic()
            if MIRROR:
                frame = cv2.flip(frame, 1)

            now = time.monotonic()
            ts = max(int(now * 1000), last_ts + 1)  # the tracker needs rising timestamps
            last_ts = ts
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
            landmarks = result.hand_landmarks[0] if result.hand_landmarks else None

            h, w = frame.shape[:2]
            label = controller.update(landmarks, w, h, now)
            # Keep a click's name on screen for a moment, so you can see it happened.
            if label in FLASH_LABELS:
                flash_label, flash_until = label, now + 0.8
            elif now < flash_until and label in ("Move", "Aim (cursor locked)", "Pinch"):
                label = flash_label
            if controller.gaps and controller.pinch_gap < AIM_LOCK_START and now - last_debug_t > 0.25:
                # Pinch numbers in the log (menu > Open log), to tune the settings for a real hand.
                g, r = controller.gaps, controller.reach
                print(f"gaps index={g['index']:.2f} middle={g['middle']:.2f} ring={g['ring']:.2f}  "
                      f"index_reach={r['index']:.2f} ring_reach={r['ring']:.2f}  -> {label}")
                last_debug_t = now

            dt = now - prev_t
            prev_t = now
            if dt > 0:
                fps = 0.9 * fps + 0.1 * (1.0 / dt)

            if SHOW_PREVIEW:
                cv2.imshow(window, draw_preview(frame, landmarks, label, fps, controller.gaps))
                if not shown:
                    bring_to_front()
                    shown = True
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), ord("Q"), 27):
                    break
                if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                    break  # preview window was closed: stop tracking too
    except KeyboardInterrupt:
        pass
    finally:
        controller.release_all()
        cap.release()
        landmarker.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
