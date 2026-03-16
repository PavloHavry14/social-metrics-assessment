# Challenge 04 -- ADB Automation: Written Explanation

## 1. State Detection Approach

State detection uses UI hierarchy parsing as the primary method, with a screenshot-based fallback. The implementation lives in `screen_detector.py`.

**How it works.** `ScreenDetector.detect()` calls `adb.dump_ui_hierarchy()`, which runs `uiautomator dump` on the device and reads back the XML. The XML contains every visible UI node with attributes like `text`, `resource-id`, `content-desc`, `class`, and `bounds`.

The detector scores each candidate `AppState` against the XML using `_SIGNATURE_PATTERNS` -- a dictionary mapping each state to a list of "signature groups." Each group is a list of `(attribute, regex)` pairs. A group matches if any node in the XML tree satisfies all the attribute-regex pairs in that group. The state with the highest number of matching groups wins.

Popups get priority: `_detect_from_xml` checks `AppState.POPUP` first because popups overlay other screens. If a dialog/modal node is present, POPUP is returned immediately regardless of what is underneath.

**Why UI hierarchy over screenshot pixel analysis.**

- **Resolution independence.** The UI hierarchy uses the same XML structure regardless of whether the device is 720p, 1080p, or 1440p. Pixel-based approaches require training data or templates per resolution, and break when system font size or DPI changes.
- **Semantic richness.** The hierarchy gives us `text`, `content-desc`, and `resource-id` values -- we can match on "Following," "For You," or a resource-id containing "profile." Pixel analysis would require OCR (slower, error-prone) or image classification models (need training data, add dependencies).
- **Lightweight.** No image processing libraries needed. The XML is parsed with Python's built-in `xml.etree.ElementTree`. No OpenCV, no Pillow, no ML models.

**Tradeoffs.**

- **`uiautomator dump` is slow (~1-2 seconds).** It freezes the UI briefly while serialising the hierarchy, which can interfere with animations. This is why we cache nothing and accept the latency.
- **Not all apps expose meaningful attributes.** Some apps use custom Views with no `content-desc` or use obfuscated `resource-id` values. The regex-based signatures would need updating per app version.
- **Dynamic content.** Feed items change as the user scrolls, so the hierarchy is only valid at the instant of the dump. If the app is mid-transition (e.g., between screens), the dump may capture an intermediate state that matches nothing, falling through to UNKNOWN.

The screenshot fallback (`_detect_from_screenshot`) is intentionally minimal: it only checks whether the target app is in the foreground via `dumpsys activity`. A production system would add template matching or a lightweight CNN here, but we avoided that to keep dependencies to zero.


## 2. Resolution Handling -- Ratio-Based Coordinate System

Every coordinate in the system is expressed as a float ratio between 0.0 and 1.0, representing a proportion of the screen's physical dimensions. The conversion to absolute pixels happens at the last possible moment, inside `ADBController._abs()`:

```python
def _abs(self, x_ratio: float, y_ratio: float) -> Tuple[int, int]:
    w, h = self.get_screen_size()
    return int(x_ratio * w), int(y_ratio * h)
```

`get_screen_size()` calls `adb shell wm size` once, parses the `Physical size: WxH` line, and caches the result. All subsequent `tap()`, `swipe()`, and `long_press()` calls pass through `_abs()`.

**Concrete example.** The like button is configured at ratio `(0.95, 0.50)` in `config.py`. On a 1080x1920 device, this becomes pixel `(1026, 960)`. On a 1440x2560 device, the same ratio yields `(1368, 1280)`. The button is in the same relative position on both screens.

**Why this works for 720p/1080p/1440p.** Most Android apps (including TikTok) use constraint-based or relative layouts. UI elements occupy the same proportional position regardless of pixel density. The like button is always on the right side, roughly vertically centred. Expressing this as `(0.95, 0.50)` captures the layout intent, not a specific pixel.

**Where it can break.** Devices with non-standard aspect ratios (foldables, tablets) shift proportions. A 21:9 phone has more vertical space relative to width, so `y_ratio=0.50` may land higher than expected. The `ScreenDetector.find_element_coords()` method mitigates this by reading the actual `bounds` attribute from the UI hierarchy XML and computing the centre of the matched element. The config ratios in `config.py` are fallbacks used only when the UI hierarchy does not expose the element.

**Bounds parsing.** `_parse_bounds` in `screen_detector.py` takes the `[x1,y1][x2,y2]` format from the XML, computes the centre point `((x1+x2)/2, (y1+y2)/2)`, and divides by the screen dimensions to produce a ratio. This means even the UI-hierarchy-derived coordinates go through the same ratio system, keeping everything consistent.


## 3. Unexpected Popup Mid-Sequence

The system handles unexpected popups at every step of every action sequence. Here is the exact recovery flow, traced through the code:

**Step 1: Detection.** Every action method in `actions.py` calls `self.handle_popup_if_present()` before doing its work. Additionally, `wait_for_state()` checks for popups during its polling loop (line 69-71 in `actions.py`): if the detector returns `AppState.POPUP` while waiting for a different target state, it calls `_dismiss_popup()` and continues polling.

**Step 2: Popup identified.** The `POPUP` state is detected via `_SIGNATURE_PATTERNS` in `screen_detector.py`. The patterns look for nodes with `resource-id` matching `dialog|modal|popup|alert`, `class` matching `android.app.Dialog`, or `text` matching known popup strings like "Rate this app", "Update available", "Login expired", "Network error."

**Step 3: Dismiss attempt.** `_dismiss_popup()` in `actions.py` first tries to locate a dismiss button by calling `self.detector.find_popup_dismiss_button()`. This method iterates through `POPUP_DISMISS_PATTERNS` -- a list of known dismiss-button signatures: text matching "Not now", "Later", "Cancel", "Dismiss", "Close", "No thanks", "Skip", or `content-desc` matching "close|dismiss|cancel". If a matching node is found, its `bounds` are parsed into ratio coordinates and tapped.

**Step 4: Fallback.** If no known dismiss button is found (the popup is unrecognised), the code falls back to pressing the Android Back button (`adb.press_back()`). This works for most modal dialogs on Android.

**Step 5: Resume.** After a 0.5-second pause, the `wait_for_state()` loop continues polling. If the popup was dismissed, the next detection cycle should return the expected target state. If the popup persists or another popup appears, the cycle repeats.

**Step 6: Timeout and recovery.** If popups keep appearing and the target state is never reached within `wait_timeout` (default 10 seconds), `wait_for_state()` returns `False`. The calling action method propagates the failure. In `automation.py`, `_with_recovery()` catches the `ActionError`, transitions the state machine to `ERROR`, and triggers the recovery sequence: up to 3 Back presses, then a force-stop and app relaunch. This is retried up to `max_recovery_attempts` (default 3) times.

**What this means in practice.** A "Rate this app" dialog mid-scroll is dismissed via its "Not now" button. A system-level "Update available" dialog gets dismissed via "Later." A completely unknown popup (e.g., a new A/B test modal TikTok deployed) gets a Back press. If even Back does not clear it, the recovery sequence force-stops the app and relaunches from scratch.


## 4. Debugging a Missed Like Tap

If the like tap consistently misses on a specific device model, here is the diagnostic process I would follow:

**Step 1: Enable debug logging and inspect coordinates.** The `ADBController.tap()` method logs the absolute pixel coordinates and the ratio that produced them: `"Tapped (%d, %d) [ratio %.2f, %.2f]"`. I would run the script with `log_level: DEBUG` and examine the logged tap coordinates for the like action. Simultaneously, I would take a screenshot (`adb.take_screenshot()`) at the moment of the tap to visually verify where the tap landed relative to the like button.

**Step 2: Check screen size detection.** Run `adb shell wm size` manually on the device. Some devices report an "Override size" (set by the user or an app) that differs from the physical size. `get_screen_size()` prefers the "Physical size" line, but if the app renders at the override resolution, the ratio-to-pixel conversion will be off. For example, if the physical size is 1440x2560 but the override is 1080x1920, our ratios compute pixels against 1440x2560 but the app's layout corresponds to 1080x1920. Fix: use the override size when present.

**Step 3: Dump the UI hierarchy and check the like button's actual bounds.** Run `adb shell uiautomator dump` and find the like button node. Compare its `bounds` attribute to where we tapped. The like button on this device model may be positioned differently due to:
- A navigation bar or status bar consuming screen space (changing effective layout height)
- A notch or camera cutout pushing content down
- The device using a non-16:9 aspect ratio (e.g., 19.5:9 on modern phones)

**Step 4: Check whether find_element_coords is being used vs the fallback.** In `actions.py`, `like_current_post()` first tries `self.detector.find_element_coords("content-desc", r"(?i)like")`. If this returns `None`, it falls back to `CONFIG["coords"]["like_button"]` which is the static ratio `(0.95, 0.50)`. If the UI hierarchy on this device does not expose a `content-desc` with "like" (e.g., TikTok uses a different attribute or the attribute is localised to another language), the fallback ratio fires and may be wrong for this device's layout.

**Step 5: Resolution.** Depending on findings:
- If the UI hierarchy exposes the button under a different attribute (e.g., `resource-id` instead of `content-desc`), add that pattern to the `find_element_coords` call.
- If the screen size override is the issue, modify `get_screen_size()` to prefer the override when one exists.
- If the aspect ratio shifts the like button's proportional position, update the fallback ratio in `config.py` or, better, add device-model-specific overrides.
- If `uiautomator dump` fails or returns incomplete data on this model (some Samsung devices have known issues), the screenshot fallback path needs to be extended with template matching for the like button icon.

The key diagnostic principle: never assume the ratio is correct. Always cross-reference the logged tap pixel against the actual element bounds from the UI hierarchy or a screenshot.
