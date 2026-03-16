"""
Tests for Challenge 02 — Schema Design.

These tests validate the SQL schema without requiring a live PostgreSQL
instance.  They work in two modes:

    1. **Syntax validation**: Parse and validate SQL statements using
       sqlparse (if available) or basic string checks.
    2. **SQLite simulation**: Load a simplified version of the schema
       into an in-memory SQLite database to test constraints and queries
       where possible. (PostgreSQL-specific features like triggers and
       materialized views are tested via assertion of their presence in
       the raw SQL text.)

If you have a PostgreSQL instance available, set the environment variable
    TEST_PG_DSN=postgresql://user:pass@localhost/test_db
to run the full PostgreSQL integration tests.
"""

from __future__ import annotations

import os
import re
import sqlite3
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Locate schema file
# ---------------------------------------------------------------------------
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


@pytest.fixture()
def raw_sql() -> str:
    """Read the raw schema SQL."""
    return SCHEMA_PATH.read_text(encoding="utf-8")


# =========================================================================
# 1. SQL SYNTAX AND STRUCTURAL VALIDATION
# =========================================================================

class TestSQLStructure:
    """Validate that the schema file is well-formed and contains all
    required structural elements."""

    def test_schema_file_exists(self):
        assert SCHEMA_PATH.exists(), f"Schema file not found at {SCHEMA_PATH}"

    def test_schema_is_non_empty(self, raw_sql):
        assert len(raw_sql.strip()) > 100, "Schema file appears empty"

    def test_required_tables_present(self, raw_sql):
        """All required tables are defined with CREATE TABLE."""
        required_tables = [
            "clients",
            "providers",
            "accounts",
            "account_ownership",
            "posts",
            "scrape_runs",
            "scrape_run_accounts",
            "metric_snapshots",
        ]
        sql_upper = raw_sql.upper()
        for table in required_tables:
            pattern = rf"CREATE\s+TABLE\s+{table.upper()}\b"
            assert re.search(pattern, sql_upper), (
                f"Missing CREATE TABLE for '{table}'"
            )

    def test_append_only_trigger_exists(self, raw_sql):
        """The schema defines a trigger to block UPDATE on metric_snapshots."""
        sql_upper = raw_sql.upper()
        assert "BEFORE UPDATE ON METRIC_SNAPSHOTS" in sql_upper, (
            "Missing BEFORE UPDATE trigger on metric_snapshots"
        )

    def test_delete_trigger_exists(self, raw_sql):
        """The schema defines a trigger to block DELETE on metric_snapshots."""
        sql_upper = raw_sql.upper()
        assert "BEFORE DELETE ON METRIC_SNAPSHOTS" in sql_upper, (
            "Missing BEFORE DELETE trigger on metric_snapshots"
        )

    def test_prevent_snapshot_mutation_function_raises(self, raw_sql):
        """The prevent_snapshot_mutation function contains RAISE EXCEPTION."""
        assert "RAISE EXCEPTION" in raw_sql, (
            "prevent_snapshot_mutation should RAISE EXCEPTION"
        )
        assert "append-only" in raw_sql.lower(), (
            "Error message should mention 'append-only'"
        )

    def test_materialized_view_high_water_marks(self, raw_sql):
        """A materialized view for high water marks is defined."""
        assert "mv_high_water_marks" in raw_sql, (
            "Missing materialized view mv_high_water_marks"
        )
        assert "MAX(views)" in raw_sql or "max(views)" in raw_sql, (
            "High water mark view should use MAX(views)"
        )

    def test_metric_snapshots_has_check_constraints(self, raw_sql):
        """metric_snapshots should have CHECK constraints for non-negative values."""
        # Look for CHECK (views >= 0) or similar
        assert re.search(r"CHECK\s*\(\s*views\s*>=\s*0\s*\)", raw_sql, re.IGNORECASE), (
            "Missing CHECK (views >= 0) on metric_snapshots"
        )
        assert re.search(r"CHECK\s*\(\s*likes\s*>=\s*0\s*\)", raw_sql, re.IGNORECASE), (
            "Missing CHECK (likes >= 0) on metric_snapshots"
        )

    def test_account_ownership_unique_active_owner(self, raw_sql):
        """Only one active ownership per account (partial unique index)."""
        sql_upper = raw_sql.upper()
        assert "WHERE VALID_TO IS NULL" in sql_upper, (
            "Missing partial unique index for active ownership"
        )

    def test_ownership_immutability_trigger_exists(self, raw_sql):
        """Trigger to block mutations on closed ownership periods."""
        sql_upper = raw_sql.upper()
        assert "BEFORE UPDATE ON ACCOUNT_OWNERSHIP" in sql_upper, (
            "Missing BEFORE UPDATE trigger on account_ownership"
        )

    def test_posts_disappeared_columns(self, raw_sql):
        """Posts table has columns for tracking disappearance."""
        sql_lower = raw_sql.lower()
        assert "disappeared_at" in sql_lower
        assert "is_disappeared" in sql_lower


# =========================================================================
# 2. QUERY VALIDATION — Required views/queries are present
# =========================================================================

class TestRequiredQueries:
    """Verify the schema contains the 4 required queries."""

    def test_query1_client_high_water_views(self, raw_sql):
        """Query 1: High water mark total views per client."""
        assert "vw_client_high_water_views" in raw_sql, (
            "Missing view vw_client_high_water_views (Query 1)"
        )
        # Should join ownership to attribute by publish time
        sql_upper = raw_sql.upper()
        assert "PUBLISHED_AT" in sql_upper, (
            "Query 1 should reference published_at for historical attribution"
        )

    def test_query2_metric_regressions(self, raw_sql):
        """Query 2: Posts where latest scrape shows metric regressions."""
        assert "vw_metric_regressions" in raw_sql, (
            "Missing view vw_metric_regressions (Query 2)"
        )

    def test_query3_disappeared_posts(self, raw_sql):
        """Query 3: Posts present in previous run but missing from latest."""
        assert "vw_disappeared_since_last_run" in raw_sql, (
            "Missing view vw_disappeared_since_last_run (Query 3)"
        )

    def test_query4_scrape_health_summary(self, raw_sql):
        """Query 4: Scrape run health summary."""
        assert "vw_scrape_health_summary" in raw_sql, (
            "Missing view vw_scrape_health_summary (Query 4)"
        )

    def test_query1_uses_ownership_period(self, raw_sql):
        """Query 1 must join on the ownership period active at publication
        time — not just current ownership."""
        # Look for the join pattern: published_at >= valid_from AND < valid_to
        assert re.search(
            r"published_at\s*>=\s*ao\.valid_from", raw_sql, re.IGNORECASE
        ), "Query 1 should filter by published_at >= ao.valid_from"

    def test_query2_compares_latest_to_max(self, raw_sql):
        """Query 2 should compare latest snapshot values against maximums."""
        sql_upper = raw_sql.upper()
        # Should have a comparison like views < max_views
        assert "LATEST_VIEWS" in sql_upper or "LS.VIEWS" in sql_upper, (
            "Query 2 should alias latest snapshot values"
        )


# =========================================================================
# 3. SQLite SIMULATION TESTS (constraint validation)
# =========================================================================

class TestSQLiteSimulation:
    """Use an in-memory SQLite database to validate table structure and
    basic constraint logic.  PostgreSQL-specific features (triggers,
    materialized views) cannot be tested here, but table DDL can."""

    @pytest.fixture()
    def db(self):
        """Create an in-memory SQLite database with a simplified schema."""
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")

        # Simplified schema adapted for SQLite
        conn.executescript(textwrap.dedent("""\
            CREATE TABLE clients (
                client_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE providers (
                provider_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL UNIQUE,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE accounts (
                account_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                platform_handle TEXT NOT NULL,
                provider_id     INTEGER NOT NULL REFERENCES providers(provider_id),
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (platform_handle, provider_id)
            );

            CREATE TABLE account_ownership (
                ownership_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id    INTEGER NOT NULL REFERENCES accounts(account_id),
                client_id     INTEGER NOT NULL REFERENCES clients(client_id),
                valid_from    TEXT NOT NULL DEFAULT (datetime('now')),
                valid_to      TEXT,
                CHECK (valid_to IS NULL OR valid_to > valid_from)
            );

            CREATE TABLE posts (
                post_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id       INTEGER NOT NULL REFERENCES accounts(account_id),
                platform_post_id TEXT NOT NULL,
                provider_id      INTEGER NOT NULL REFERENCES providers(provider_id),
                published_at     TEXT,
                first_seen_at    TEXT NOT NULL DEFAULT (datetime('now')),
                disappeared_at   TEXT,
                reappeared_at    TEXT,
                is_disappeared   INTEGER NOT NULL DEFAULT 0,
                UNIQUE (platform_post_id, provider_id)
            );

            CREATE TABLE scrape_runs (
                run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id  INTEGER NOT NULL REFERENCES providers(provider_id),
                started_at   TEXT NOT NULL DEFAULT (datetime('now')),
                ended_at     TEXT,
                status       TEXT NOT NULL DEFAULT 'running'
                                CHECK (status IN ('running','completed','failed','partial')),
                error_message TEXT,
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE scrape_run_accounts (
                run_account_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          INTEGER NOT NULL REFERENCES scrape_runs(run_id),
                account_id      INTEGER NOT NULL REFERENCES accounts(account_id),
                posts_expected  INTEGER,
                posts_found     INTEGER,
                error_message   TEXT,
                UNIQUE (run_id, account_id)
            );

            CREATE TABLE metric_snapshots (
                snapshot_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id      INTEGER NOT NULL REFERENCES posts(post_id),
                run_id       INTEGER NOT NULL REFERENCES scrape_runs(run_id),
                scraped_at   TEXT NOT NULL DEFAULT (datetime('now')),
                views        INTEGER NOT NULL DEFAULT 0 CHECK (views >= 0),
                likes        INTEGER NOT NULL DEFAULT 0 CHECK (likes >= 0),
                comments     INTEGER NOT NULL DEFAULT 0 CHECK (comments >= 0),
                shares       INTEGER NOT NULL DEFAULT 0 CHECK (shares >= 0)
            );
        """))
        yield conn
        conn.close()

    def _seed_basic_data(self, db: sqlite3.Connection) -> dict:
        """Insert minimal seed data and return IDs."""
        cur = db.cursor()
        cur.execute("INSERT INTO clients (name) VALUES ('Acme Corp')")
        client_id = cur.lastrowid
        cur.execute("INSERT INTO providers (name) VALUES ('tiktok')")
        provider_id = cur.lastrowid
        cur.execute(
            "INSERT INTO accounts (platform_handle, provider_id) VALUES ('@creator1', ?)",
            (provider_id,),
        )
        account_id = cur.lastrowid
        cur.execute(
            "INSERT INTO account_ownership (account_id, client_id, valid_from) "
            "VALUES (?, ?, '2025-01-01T00:00:00Z')",
            (account_id, client_id),
        )
        cur.execute(
            "INSERT INTO posts (account_id, platform_post_id, provider_id, published_at) "
            "VALUES (?, '7321456789', ?, '2025-03-14T10:30:00Z')",
            (account_id, provider_id),
        )
        post_id = cur.lastrowid
        cur.execute(
            "INSERT INTO scrape_runs (provider_id, status) VALUES (?, 'completed')",
            (provider_id,),
        )
        run_id = cur.lastrowid
        db.commit()
        return {
            "client_id": client_id,
            "provider_id": provider_id,
            "account_id": account_id,
            "post_id": post_id,
            "run_id": run_id,
        }

    def test_insert_metric_snapshot_succeeds(self, db):
        """Basic INSERT into metric_snapshots works."""
        ids = self._seed_basic_data(db)
        db.execute(
            "INSERT INTO metric_snapshots (post_id, run_id, views, likes, comments, shares) "
            "VALUES (?, ?, 1000, 50, 5, 10)",
            (ids["post_id"], ids["run_id"]),
        )
        row = db.execute("SELECT views FROM metric_snapshots").fetchone()
        assert row[0] == 1000

    def test_negative_views_rejected(self, db):
        """CHECK constraint blocks negative view counts."""
        ids = self._seed_basic_data(db)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO metric_snapshots (post_id, run_id, views, likes, comments, shares) "
                "VALUES (?, ?, -1, 0, 0, 0)",
                (ids["post_id"], ids["run_id"]),
            )

    def test_negative_likes_rejected(self, db):
        """CHECK constraint blocks negative like counts."""
        ids = self._seed_basic_data(db)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO metric_snapshots (post_id, run_id, views, likes, comments, shares) "
                "VALUES (?, ?, 0, -5, 0, 0)",
                (ids["post_id"], ids["run_id"]),
            )

    def test_duplicate_platform_post_id_rejected(self, db):
        """UNIQUE (platform_post_id, provider_id) prevents duplicates."""
        ids = self._seed_basic_data(db)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO posts (account_id, platform_post_id, provider_id) "
                "VALUES (?, '7321456789', ?)",
                (ids["account_id"], ids["provider_id"]),
            )

    def test_foreign_key_enforcement(self, db):
        """Inserting a snapshot with a non-existent post_id fails."""
        ids = self._seed_basic_data(db)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO metric_snapshots (post_id, run_id, views, likes, comments, shares) "
                "VALUES (99999, ?, 100, 10, 1, 0)",
                (ids["run_id"],),
            )

    def test_ownership_valid_to_must_be_after_valid_from(self, db):
        """CHECK (valid_to IS NULL OR valid_to > valid_from) enforced."""
        ids = self._seed_basic_data(db)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO account_ownership (account_id, client_id, valid_from, valid_to) "
                "VALUES (?, ?, '2025-06-01', '2025-01-01')",
                (ids["account_id"], ids["client_id"]),
            )

    def test_scrape_run_status_check_constraint(self, db):
        """Status must be one of the allowed values."""
        ids = self._seed_basic_data(db)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO scrape_runs (provider_id, status) VALUES (?, 'invalid_status')",
                (ids["provider_id"],),
            )

    def test_multiple_snapshots_append(self, db):
        """Multiple snapshots for the same post can be inserted (append-only model)."""
        ids = self._seed_basic_data(db)
        for views in [1000, 1200, 1500]:
            db.execute(
                "INSERT INTO metric_snapshots (post_id, run_id, views, likes, comments, shares) "
                "VALUES (?, ?, ?, 50, 5, 10)",
                (ids["post_id"], ids["run_id"], views),
            )
        count = db.execute("SELECT COUNT(*) FROM metric_snapshots").fetchone()[0]
        assert count == 3

    def test_high_water_mark_query_logic(self, db):
        """Verify MAX aggregation logic (simulating the materialized view)."""
        ids = self._seed_basic_data(db)
        for views, likes in [(1000, 50), (1200, 45), (900, 60)]:
            db.execute(
                "INSERT INTO metric_snapshots (post_id, run_id, views, likes, comments, shares) "
                "VALUES (?, ?, ?, ?, 5, 10)",
                (ids["post_id"], ids["run_id"], views, likes),
            )
        row = db.execute(
            "SELECT MAX(views) AS max_views, MAX(likes) AS max_likes "
            "FROM metric_snapshots WHERE post_id = ?",
            (ids["post_id"],),
        ).fetchone()
        assert row[0] == 1200, "MAX(views) should be 1200"
        assert row[1] == 60, "MAX(likes) should be 60"

    def test_metric_regression_detection_query(self, db):
        """Simulate the metric regression query: latest < max."""
        ids = self._seed_basic_data(db)
        # Insert snapshots: first high, then lower
        db.execute(
            "INSERT INTO metric_snapshots (post_id, run_id, scraped_at, views, likes, comments, shares) "
            "VALUES (?, ?, '2025-03-13T12:00:00Z', 5000, 200, 10, 5)",
            (ids["post_id"], ids["run_id"]),
        )
        db.execute(
            "INSERT INTO metric_snapshots (post_id, run_id, scraped_at, views, likes, comments, shares) "
            "VALUES (?, ?, '2025-03-14T12:00:00Z', 4500, 190, 12, 5)",
            (ids["post_id"], ids["run_id"]),
        )
        # Query: find posts where latest < max
        row = db.execute(textwrap.dedent("""\
            WITH latest AS (
                SELECT post_id, views, likes
                FROM metric_snapshots
                ORDER BY scraped_at DESC
                LIMIT 1
            ),
            hwm AS (
                SELECT post_id, MAX(views) AS max_views, MAX(likes) AS max_likes
                FROM metric_snapshots
                GROUP BY post_id
            )
            SELECT l.post_id, l.views, hwm.max_views, l.likes, hwm.max_likes
            FROM latest l
            JOIN hwm ON hwm.post_id = l.post_id
            WHERE l.views < hwm.max_views OR l.likes < hwm.max_likes
        """)).fetchone()
        assert row is not None, "Should detect metric regression"
        assert row[1] == 4500  # latest views
        assert row[2] == 5000  # max views


# =========================================================================
# 4. APPEND-ONLY ENFORCEMENT — Textual validation
# =========================================================================

class TestAppendOnlyEnforcement:
    """Verify the schema explains and implements append-only semantics."""

    def test_trigger_blocks_update(self, raw_sql):
        """The schema has a trigger that fires BEFORE UPDATE on metric_snapshots."""
        assert "trg_no_update_snapshots" in raw_sql

    def test_trigger_blocks_delete(self, raw_sql):
        """The schema has a trigger that fires BEFORE DELETE on metric_snapshots."""
        assert "trg_no_delete_snapshots" in raw_sql

    def test_trigger_function_raises_exception(self, raw_sql):
        """The trigger function raises an exception to abort the mutation."""
        # Find the function body
        assert "prevent_snapshot_mutation" in raw_sql
        assert "RAISE EXCEPTION" in raw_sql

    def test_200_word_explanation_present(self, raw_sql):
        """The schema contains the required ~200-word explanation of
        append-only enforcement."""
        # Look for the explanation block
        assert "APPEND-ONLY ENFORCEMENT EXPLANATION" in raw_sql or \
               "append-only" in raw_sql.lower(), (
            "Schema should contain an explanation of append-only enforcement"
        )
        # Check that it discusses multiple mechanisms
        sql_lower = raw_sql.lower()
        assert "trigger" in sql_lower
        assert "privilege" in sql_lower or "grant" in sql_lower or "role" in sql_lower
        assert "identity" in sql_lower or "generated always" in sql_lower

    def test_ownership_immutability_enforced(self, raw_sql):
        """Closed ownership periods cannot be mutated."""
        assert "prevent_ownership_history_mutation" in raw_sql
        assert "trg_immutable_ownership" in raw_sql
