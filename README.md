# Social Metrics Assessment

Expert-level engineering assessment covering data integrity, device automation, and live debugging across 7 challenges.

**Stack:** Python + PostgreSQL | **Tests:** 149 passing

---

## Challenges

### Part A: Data Integrity & Pipeline Engineering

| Challenge | Description | Files | Tests |
|-----------|-------------|-------|-------|
| **01 — Reconciliation Engine** | Merges metrics from two API providers with URL normalization, truncation-aware caption matching, monotonic metric enforcement, and disappeared post tracking | `challenge-01/` | 44 |
| **02 — Schema Design** | Append-only PostgreSQL schema with SCD Type 2 ownership, high water mark queries, and structurally enforced data integrity (triggers block UPDATE/DELETE on snapshots) | `challenge-02/` | 32 |
| **03 — Queue Worker** | Crash-safe metric refresh worker using atomic transaction batching. If the process dies mid-write, the transaction rolls back — zero partial data. Includes exponential backoff, batch monitoring, and stale job detection | `challenge-03/` | 15 |

### Part B: Android Device Automation & Scripting

| Challenge | Description | Files | Tests |
|-----------|-------------|-------|-------|
| **04 — ADB Automation** | State machine-driven automation script with resolution-independent coordinates, adaptive waits (no fixed sleeps), popup handling, and error recovery | `challenge-04/` | 27 |
| **05 — Device Compliance Validator** | 10-check validator using real ADB commands. Handles the stale MCC trap, locale property disagreements, background location permission leaks, and hidden saved WiFi networks | `challenge-05/` | 20 |
| **06 — Fleet Orchestrator** | Safe rollout system with wave planning (canary → progressive), automatic rollback on failure, connection drop handling, and real-time status dashboard | `challenge-06/` | 11 |

### Part C: Live Debugging

| Challenge | Description | Files |
|-----------|-------------|-------|
| **07 — The 1.3M View Gap** | Structured investigation of a 1.3M view discrepancy. 7 ranked root causes, exact diagnostic queries for top 3, and permanent fix proposals | `challenge-07/` |

---

## Project Structure

```
social-metrics-assessment/
├── challenge-01/           # Reconciliation Engine
│   ├── reconciler.py       # Main reconciliation pipeline
│   ├── url_normalizer.py   # Share URL → canonical URL mapping
│   ├── models.py           # Data classes (PostStatus, ReconciledPost, etc.)
│   ├── test_reconciler.py  # 44 tests
│   └── EXPLANATION.md      # Written explanation (graded)
├── challenge-02/           # Schema Design
│   ├── schema.sql          # CREATE TABLE, triggers, views, queries
│   └── test_schema.py      # 32 tests (structure + logic validation)
├── challenge-03/           # Queue Worker
│   ├── worker.py           # Crash-safe worker with atomic writes
│   ├── providers.py        # Provider API client abstraction
│   ├── retry_logic.py      # Backoff, jitter, failure tracking
│   └── test_worker.py      # 15 tests
├── challenge-04/           # ADB Automation
│   ├── automation.py       # Main orchestrator + CLI
│   ├── state_machine.py    # App state enum + transitions
│   ├── screen_detector.py  # UI hierarchy parsing for state detection
│   ├── adb_controller.py   # Resolution-independent ADB wrapper
│   ├── actions.py          # Scroll/like/post action sequences
│   ├── config.py           # Tunable parameters
│   ├── test_automation.py  # 27 tests
│   └── EXPLANATION.md      # Written explanation (graded)
├── challenge-05/           # Device Compliance
│   ├── adb_runner.py       # ADB command executor (10 checks)
│   ├── validator.py        # Pass/fail/warning evaluator
│   └── test_compliance.py  # 20 tests
├── challenge-06/           # Fleet Orchestrator
│   ├── orchestrator.py     # Main rollout loop
│   ├── wave_planner.py     # Wave sizing + progression
│   ├── device_ops.py       # Preflight/execute/rollback operations
│   ├── dashboard.py        # Real-time status output
│   └── test_orchestrator.py # 11 tests
├── challenge-07/           # Investigation
│   └── investigation.md    # 1.3M view gap analysis (graded)
└── research/               # Supporting research
    ├── tiktok-url-formats.md
    └── adb-commands.md
```

---

## Running Tests

```bash
# All tests
pytest

# Individual challenges
pytest challenge-01/
pytest challenge-02/
pytest challenge-03/
pytest challenge-04/
pytest challenge-05/
pytest challenge-06/
```

---

## Key Design Decisions

**Challenge 01 — Share URL Resolution:** TikTok share URLs (`vm.tiktok.com/...`) don't contain the video ID and can't be resolved without HTTP requests. Instead of making network calls during reconciliation, the engine matches unresolved share URLs using secondary signals: same account + timestamp proximity + caption similarity.

**Challenge 02 — Append-Only Enforcement:** `BEFORE UPDATE` and `BEFORE DELETE` triggers on `metric_snapshots` raise exceptions, making overwrites structurally impossible at the database level — not just an application convention.

**Challenge 03 — Crash-Safe Writes:** All metric snapshots for a scrape run are written in a single atomic transaction. If the process crashes mid-write, the entire transaction rolls back. Retry creates a fresh `scrape_run_id` and writes from scratch — no duplicates, no partial data.

**Challenge 04 — State Detection:** UI hierarchy XML parsing (`uiautomator dump`) over screenshot pixel analysis. It's more resilient to theme changes and provides semantic element information. Coordinates use ratios (0.0–1.0) converted to absolute pixels at runtime for resolution independence.

**Challenge 06 — Wave Strategy:** Canary wave (5 devices, one per Android version) must fully succeed before any broader rollout. Subsequent waves scale geometrically with a 2% failure rate threshold. Interrupted devices (connection drops) are excluded from failure rate calculations.
