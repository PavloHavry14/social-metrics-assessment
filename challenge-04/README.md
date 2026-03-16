# Challenge 04: ADB Automation with State Machine

Social-media app automation via ADB, driven by a finite state machine with
resolution-independent coordinates, XML-based screen detection, and automatic
error recovery.

---

## 1. System Architecture

```
+------------------------------------------------------------------+
|                        Automation                                 |
|  (orchestrator: launch, scroll-like, post, error recovery)        |
+--------+---------------------------------------------------------+
         |
         |  delegates to
         v
+------------------------------------------------------------------+
|                         Actions                                   |
|  (scroll_feed, like_current_post, full_post_sequence,             |
|   wait_for_state, handle_popup_if_present)                        |
+--------+----------------+----------------+-----------------------+
         |                |                |
         | taps/swipes    | detect()       | transition()
         v                v                v
+----------------+ +----------------+ +------------------+
| ADBController  | | ScreenDetector | |   StateMachine   |
| - tap(rx, ry)  | | - XML parsing  | | - current state  |
| - swipe()      | | - signatures   | | - history        |
| - input_text() | | - popup detect | | - valid checks   |
| - press_back() | | - screenshot   | |                  |
| - launch_app() | |   fallback     | |                  |
| - force_stop() | +-------+--------+ +------------------+
+-------+--------+         |
        |                  | uiautomator dump
        |  adb shell       | dump_ui_hierarchy()
        v                  v
+------------------------------------------------------------------+
|                     Android Device                                |
|  (physical or emulator, any resolution)                           |
+------------------------------------------------------------------+
```

---

## 2. State Machine

All states and valid transitions. Every state can reach POPUP and ERROR
(omitted from arrows for clarity; shown in the legend).

```
                        +-------------------+
                        |      UNKNOWN      |
                        +-------------------+
                       / |         |         \
                      v  v         v          v
          +-----------+ +--------+ +---------+ +-------+
          | HOME_FEED | |PROFILE | | UPLOAD  | |POPUP  |
          +-----------+ +--------+ +---------+ +-------+
            |       ^    ^  |        |   ^       | | | |
            |       |    |  |        |   |       | | | |
            |       +----+  |        v   |       v v v v
            |                |  +----------------+  (any
            |                |  |CAPTION_EDITOR  |  normal
            |                |  +----------------+  screen)
            |                |        |
            v                |        v
          +-----------+      |  +-----------+
          |   ERROR   |------+  |  POSTING  |
          +-----------+         +-----------+
            |                        |
            v                        v
          UNKNOWN             +---------------+
          HOME_FEED           | POST_COMPLETE |
                              +---------------+
                                |           |
                                v           v
                            HOME_FEED    PROFILE

Legend:
  - POPUP and ERROR are reachable from every non-ERROR state
  - POPUP can return to any normal screen (including itself)
  - ERROR can return to HOME_FEED or UNKNOWN
```

### Transition Table (complete)

```
+-----------------+------------------------------------------------------+
|   From          |   To (valid targets)                                  |
+-----------------+------------------------------------------------------+
| UNKNOWN         | HOME_FEED, PROFILE, UPLOAD, POPUP, ERROR             |
| HOME_FEED       | PROFILE, UPLOAD, POPUP, ERROR                        |
| PROFILE         | HOME_FEED, UPLOAD, POPUP, ERROR                      |
| UPLOAD          | HOME_FEED, CAPTION_EDITOR, POPUP, ERROR              |
| CAPTION_EDITOR  | UPLOAD, POSTING, POPUP, ERROR                        |
| POSTING         | POST_COMPLETE, ERROR, POPUP                          |
| POST_COMPLETE   | HOME_FEED, PROFILE, POPUP, ERROR                     |
| POPUP           | HOME_FEED, PROFILE, UPLOAD, CAPTION_EDITOR,          |
|                 | POSTING, POST_COMPLETE, POPUP, ERROR, UNKNOWN        |
| ERROR           | HOME_FEED, UNKNOWN                                   |
+-----------------+------------------------------------------------------+
```

---

## 3. Screen Detection Flow

```
detect()
  |
  v
+----------------------------+
| adb.dump_ui_hierarchy()    |
| (uiautomator dump + cat)   |
+----------------------------+
  |
  |  XML string (or empty)
  v
+----------------------------+     +-----------------------------+
| XML received?              |---->| NO: _detect_from_screenshot |
| _parse_xml()               |     |   is_app_foreground()?      |
+----------------------------+     |   yes -> None (inconclusive)|
  |                                |   no  -> ERROR              |
  | parsed OK                      +-----------------------------+
  v
+----------------------------+
| CHECK POPUP FIRST          |  <-- overlay priority
| _state_matches(root, POPUP)|
+----------------------------+
  |              |
  | matched      | not matched
  v              v
POPUP     +----------------------------------+
          | Score remaining states            |
          | For each state in SIGNATURES:     |
          |   score = count of matching groups |
          +----------------------------------+
                    |
                    v
          +----------------------------------+
          | Any scores > 0?                  |
          |   YES -> return highest-scoring  |
          |   NO  -> return None (-> UNKNOWN)|
          +----------------------------------+

Signature Matching Detail:
+-----------------------------------------------------+
|  _SIGNATURE_PATTERNS[state] = list of groups         |
|  Each group = [(attr, regex), (attr, regex), ...]    |
|                                                      |
|  For each XML <node>:                                |
|    if ALL (attr, regex) pairs in group match          |
|    -> group matches                                  |
|                                                      |
|  Score = number of groups that matched                |
|  Highest score wins                                  |
+-----------------------------------------------------+
```

---

## 4. Resolution-Independent Coordinate System

All coordinates are ratios in `(0.0 .. 1.0)`. The `_abs()` method converts
to device-specific pixels at execution time.

```
  Ratio Input             _abs(rx, ry)              Absolute Pixels
+----------------+    +------------------+    +-----------------------+
| rx = 0.50      | -> | x = rx * width   | -> | Device-specific value |
| ry = 0.75      |    | y = ry * height  |    |                       |
+----------------+    +------------------+    +-----------------------+

Examples for tap(0.50, 0.75):

+---------------------+-----------+-----------+--------------+
| Device Resolution   |   Width   |  Height   |  Pixels (x,y)|
+---------------------+-----------+-----------+--------------+
| 720p   (720x1280)   |    720    |   1280    |  (360,  960) |
| 1080p (1080x1920)   |   1080    |   1920    |  (540, 1440) |
| 1440p (1440x2560)   |   1440    |   2560    |  (720, 1920) |
+---------------------+-----------+-----------+--------------+

Same ratio, different devices:

     720p               1080p               1440p
  +---------+        +-----------+       +-------------+
  |         |        |           |       |             |
  |         |        |           |       |             |
  |         |        |           |       |             |
  |    *    |        |     *     |       |      *      |
  | (360,   |        |  (540,   |       |   (720,    |
  |  960)   |        |   1440)  |       |    1920)   |
  |         |        |           |       |             |
  +---------+        +-----------+       +-------------+

  * = tap target at ratio (0.50, 0.75)
```

---

## 5. Error Recovery Flow

```
Action (scroll_and_like, full_post_sequence, ...)
  |
  | raises ActionError or ADBError
  v
+---------------------------------------+
| _with_recovery(label, fn)             |
| attempt = 1                           |
+---------------------------------------+
  |
  v
+---------------------------------------+
| Try fn(*args)                         |
+---------------------------------------+
  |               |
  | success       | exception
  v               v
RETURN       +---------------------------+
             | sm.transition(ERROR)      |
             | attempt < max_attempts?   |
             +---------------------------+
               |            |
               | yes        | no -> raise ActionError
               v
         +-----------------------------+
         | _recover()                  |
         +-----------------------------+
               |
               v
         +-----------------------------+
         | Step 1: Press Back          |
         | (up to back_press_limit)    |
         +----+------------------------+
              |
              | after each press: detect()
              v
         +-----------------------------+
         | Reached HOME_FEED?          |
         +-----------------------------+
           |             |
           | yes         | no (exhausted limit)
           v             v
         RETRY    +-----------------------------+
                  | Step 2: force_stop(pkg)     |
                  |          launch_app(pkg)     |
                  +-----------------------------+
                           |
                           v
                  +-----------------------------+
                  | wait_for_state(HOME_FEED,   |
                  |   timeout=15s)              |
                  +-----------------------------+
                    |             |
                    | success     | timeout
                    v             v
                  RETRY       ABORT (break)
```

---

## 6. Scroll & Like Sequence

```
scroll_and_like(count)
  |
  v
+------------------------------+
| Current state == HOME_FEED?  |
+------------------------------+
  |             |
  | yes         | no
  |             v
  |     +-------------------+
  |     | go_home()         |
  |     | -> tap home_tab   |
  |     | -> wait HOME_FEED |
  |     +-------------------+
  |             |
  v             v
+------------------------------+
| for i in range(count):       |
+------------------------------+
  |
  v
+------------------------------+
| handle_popup_if_present()    |  <-- check before every action
+------------------------------+
  |
  v
+------------------------------+
| scroll_feed()                |
|   swipe(start -> end)        |
|   duration = random(range)   |
|   pause = random(range)      |
+------------------------------+
  |
  v
+------------------------------+
| like_current_post()          |
|   1. find "like" via XML     |
|   2. fallback: config coords |
|   3. tap(coords)             |
+------------------------------+
  |
  v
+------------------------------+
| liked += 1                   |
| loop or return liked count   |
+------------------------------+
```

---

## 7. Post Sequence

```
full_post_sequence(caption)
  |
  v
+---------------------------------+
| Step 1: open_upload()           |
|   tap upload_button             |
|   wait_for_state(UPLOAD)        |
+---------------------------------+
  |
  v
+---------------------------------+
| Step 2: select_video()          |
|   tap first gallery thumbnail   |
|   find "Next" button via XML    |
|   tap Next                      |
|   wait_for_state(CAPTION_EDITOR)|
+---------------------------------+
  |
  v
+---------------------------------+
| Step 3: enter_caption(text)     |
|   find caption field via XML    |
|   fallback: config coords       |
|   tap field -> input_text()     |
+---------------------------------+
  |
  v
+---------------------------------+
| Step 4: submit_post()           |
|   find "Post" button via XML    |
|   tap Post                      |
|   wait_for_state(POSTING)       |
|   wait_for_state(POST_COMPLETE, |
|     timeout=60s)                |
+---------------------------------+
  |
  v
+---------------------------------+
| verify_post_on_profile()        |
|   go_profile()                  |
|   (best-effort confirmation)    |
+---------------------------------+

Each step checks handle_popup_if_present() before acting.
Any step failure raises ActionError, caught by _with_recovery().
```

---

## Key Design Decisions

- **POPUP checked first** in screen detection because popups overlay other
  screens. Without this, a dialog over the home feed would be misidentified
  as HOME_FEED.
- **`_SIGNATURE_PATTERNS`** maps each `AppState` to a list of signature
  groups, each group being a list of `(attribute, regex)` pairs. A node
  matches a group only if *all* pairs match.
- **Adaptive waits** via `wait_for_state()`: polls `detect()` at
  `poll_interval` until `timeout`, handling popups encountered during the
  wait.
- **Popup dismissal**: find a known dismiss button (Not Now, Later, Cancel,
  etc.) via `POPUP_DISMISS_PATTERNS`. If no button found, fallback to
  pressing Back.
