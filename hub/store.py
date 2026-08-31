"""Persistence layer for GitModel Hub (SQLite).

Stores three things:

* the long-lived Copilot OAuth token (single row),
* locally issued hub API keys (used by Codex / Claude Code / curl clients),
* per-request token usage records for statistics.

A small connection-per-call model keeps things thread-safe under the FastAPI
threadpool without needing an async DB driver.
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    key        TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0
);

-- Legacy usage rows, written by builds that predate authoritative nano_aiu
-- billing. Retained read-only for the historical record; no query reads it.
-- Its rows cannot carry a meaningful nano_aiu -- nothing collected one at the
-- time -- and a 0 there is indistinguishable from the upstream genuinely
-- pricing a request at zero. That ambiguity is why new traffic goes to
-- usage_v2 rather than to more columns here, and why this table is
-- deliberately not extended with the newer billing columns.
CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    api_key       TEXT,
    model         TEXT,
    served_model  TEXT,
    endpoint      TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens  INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    streamed      INTEGER NOT NULL DEFAULT 0,
    estimated     INTEGER NOT NULL DEFAULT 0,
    status        INTEGER NOT NULL DEFAULT 200
);

CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage(model);

-- Current usage table. Every row here was written by a build that records
-- Copilot's authoritative nano_aiu charge, so nano_aiu = 0 unambiguously
-- means "the upstream priced this at zero" (a quota-billed model) rather
-- than "we didn't look". That single guarantee is why this is a separate
-- table rather than more columns on `usage`.
--
-- api_keys and kv are deliberately NOT split: existing API keys, the admin
-- credentials and the OAuth token all keep working across this change.
CREATE TABLE IF NOT EXISTS usage_v2 (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    api_key       TEXT,
    model         TEXT,
    served_model  TEXT,
    endpoint      TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens  INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    nano_aiu      INTEGER NOT NULL DEFAULT 0,
    streamed      INTEGER NOT NULL DEFAULT 0,
    estimated     INTEGER NOT NULL DEFAULT 0,
    status        INTEGER NOT NULL DEFAULT 200
);

CREATE INDEX IF NOT EXISTS idx_usage_v2_ts ON usage_v2(ts);
CREATE INDEX IF NOT EXISTS idx_usage_v2_model ON usage_v2(model);
"""

# The table all reads and writes go through. `usage` still exists and still
# holds its rows, but nothing reads it: its nano_aiu=0 is ambiguous (see the
# schema comment), so blending it into current figures would misreport both
# equivalent cost and quota counts.
USAGE_TABLE = "usage_v2"


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    db = get_settings().db_path
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA journal_mode={get_settings().journal_mode};")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)
        _migrate(c)


def _migrate(c: sqlite3.Connection) -> None:
    """Apply in-place schema upgrades for existing databases.

    This only patches up the legacy `usage` table, and only for columns that
    predate the split -- it is deliberately not extended with the newer billing
    columns (see the schema comment). New traffic lands in `usage_v2`, which is
    created complete by the schema script and needs no migration.
    """
    cols = {row["name"] for row in c.execute("PRAGMA table_info(usage)")}
    if "cached_tokens" not in cols:
        c.execute(
            "ALTER TABLE usage ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0"
        )


# --------------------------------------------------------------------------- #
# OAuth token (key/value)
# --------------------------------------------------------------------------- #
def get_oauth_token() -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key='oauth_token'").fetchone()
        return row["value"] if row else None


def set_oauth_token(token: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO kv(key, value) VALUES('oauth_token', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (token,),
        )


def clear_oauth_token() -> None:
    with _conn() as c:
        c.execute("DELETE FROM kv WHERE key='oauth_token'")


# --------------------------------------------------------------------------- #
# Generic settings (kv) + admin credentials
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: str | None = None) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def _hash_password(password: str, salt: str) -> str:
    import hashlib

    return hashlib.sha256((salt + ":" + password).encode("utf-8")).hexdigest()


def ensure_admin_defaults() -> None:
    """Seed default admin/admin credentials on first run."""
    if get_setting("admin_username") is None:
        set_setting("admin_username", "admin")
    if get_setting("admin_pw_salt") is None or get_setting("admin_pw_hash") is None:
        salt = secrets.token_hex(8)
        set_setting("admin_pw_salt", salt)
        set_setting("admin_pw_hash", _hash_password("admin", salt))


def get_admin_username() -> str:
    return get_setting("admin_username", "admin") or "admin"


def verify_admin(username: str, password: str) -> bool:
    if username != get_admin_username():
        return False
    salt = get_setting("admin_pw_salt") or ""
    expected = get_setting("admin_pw_hash") or ""
    return bool(expected) and secrets.compare_digest(
        _hash_password(password, salt), expected
    )


def set_admin_credentials(username: str, password: str) -> None:
    salt = secrets.token_hex(8)
    set_setting("admin_username", username)
    set_setting("admin_pw_salt", salt)
    set_setting("admin_pw_hash", _hash_password(password, salt))


def get_require_auth(default: bool) -> bool:
    val = get_setting("require_auth")
    if val is None:
        return default
    return val == "1"


def set_require_auth(enabled: bool) -> None:
    set_setting("require_auth", "1" if enabled else "0")


# --------------------------------------------------------------------------- #
# Azure image backend config (endpoint / api key / default model) — JSON blob
# --------------------------------------------------------------------------- #
def get_image_config() -> dict[str, str]:
    """Return the saved Azure image backend config (any field may be empty)."""
    import json

    raw = get_setting("image_config")
    if raw:
        try:
            cfg = json.loads(raw)
            if isinstance(cfg, dict):
                return {
                    "endpoint": str(cfg.get("endpoint") or ""),
                    "api_key": str(cfg.get("api_key") or ""),
                    "model": str(cfg.get("model") or ""),
                }
        except (ValueError, TypeError):
            pass
    return {"endpoint": "", "api_key": "", "model": ""}


def set_image_config(
    endpoint: str | None, api_key: str | None, model: str | None
) -> dict[str, str]:
    """Persist the Azure image backend config.

    A blank/None ``api_key`` preserves the previously stored key, so the portal
    never has to re-echo the secret back to the browser to keep it.
    """
    import json

    current = get_image_config()
    new_key = (api_key or "").strip()
    cfg = {
        "endpoint": (endpoint or "").strip(),
        "api_key": new_key or current.get("api_key", ""),
        "model": (model or "").strip(),
    }
    set_setting("image_config", json.dumps(cfg))
    return cfg


# --------------------------------------------------------------------------- #
# Pricing (USD per 1M tokens, split input / output) — used for cost estimates
# --------------------------------------------------------------------------- #
# Public list prices (USD per 1M tokens), used ONLY as a fallback for requests
# where Copilot reported no nano_aiu charge -- those models bill against the
# flat Copilot premium-request quota and report an all-zero rate, so there is
# no upstream figure to convert.
#
# Everything else is billed from the authoritative upstream nano_aiu instead,
# so entries for those models are reference/display only. The rates below were
# measured from live copilot_usage payloads (cost_per_batch / batch_size,
# divided by 1e5 to reach USD per 1M tokens), not copied from a price page.
#
# Note the zero-rate set is NOT vendor-aligned: gpt-4o and gpt-4.1 report zero
# while gpt-4 ($2.50/$10) and gpt-4-0125-preview ($10/$30) do not.
#
# There is deliberately no "default" entry. An unlisted model contributes
# nothing to equivalent cost rather than being priced at a generic rate -- see
# row_equivalent_cost: inventing a rate for quota-billed traffic would report a
# per-token cost for requests that have no per-token price, on top of already
# counting them under quota_requests.
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    # --- Anthropic (measured) ---
    "claude-sonnet-4.5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4.6": {"input": 3.0, "output": 15.0},
    # sonnet-5 bills at its $2/$10 introductory rate, not the $3/$15 sticker.
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
    "claude-opus-4.6": {"input": 5.0, "output": 25.0},
    "claude-opus-4.7": {"input": 5.0, "output": 25.0},
    "claude-opus-4.8": {"input": 5.0, "output": 25.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-haiku-4.5": {"input": 1.0, "output": 5.0},
    # --- OpenAI, /responses family (measured) ---
    "gpt-5.6-sol": {"input": 5.0, "output": 30.0},
    "gpt-5.6-terra": {"input": 2.0, "output": 12.0},
    "gpt-5.6-luna": {"input": 0.20, "output": 1.20},
    "gpt-5.5": {"input": 5.0, "output": 30.0},
    "gpt-5.4": {"input": 2.5, "output": 15.0},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.5},
    "gpt-5.3-codex": {"input": 1.75, "output": 14.0},
    # --- Azure OpenAI models that do NOT report a zero rate (measured) ---
    "gpt-4": {"input": 2.5, "output": 10.0, "cache_read": 1.25},
    "gpt-4-0613": {"input": 2.5, "output": 10.0, "cache_read": 1.25},
    "gpt-4-0125-preview": {"input": 10.0, "output": 30.0},
    "gpt-5-mini": {"input": 0.25, "output": 2.0},
    # --- Google (measured) ---
    "gemini-3.1-pro-preview": {"input": 2.0, "output": 12.0},
    "gemini-3.5-flash": {"input": 1.5, "output": 9.0},
    "gemini-3.6-flash": {"input": 1.5, "output": 7.5},
    # --- Others (measured) ---
    "grok-4.5": {"input": 2.0, "output": 6.0, "cache_read": 0.5},
    "mai-code-1-flash-picker": {"input": 0.75, "output": 4.5},
    # Azure OpenAI image model (token-based; image output tokens dominate).
    "gpt-image-2": {"input": 5.0, "output": 40.0},
}


def get_pricing() -> dict[str, dict[str, float]]:
    """Return the saved pricing table merged over the defaults.

    Each model has ``input`` / ``output`` and an optional ``cache_read`` rate
    (USD per 1M tokens). When ``cache_read`` is absent it defaults to
    ``input * 0.1`` — matching Anthropic's cache-hit pricing (0.1x base input).
    """
    import json

    pricing = {k: dict(v) for k, v in DEFAULT_PRICING.items()}
    raw = get_setting("pricing")
    if raw:
        try:
            saved = json.loads(raw)
            if isinstance(saved, dict):
                for model, rates in saved.items():
                    if isinstance(rates, dict):
                        entry = {
                            "input": float(rates.get("input", 0) or 0),
                            "output": float(rates.get("output", 0) or 0),
                        }
                        if rates.get("cache_read") is not None:
                            entry["cache_read"] = float(rates.get("cache_read") or 0)
                        pricing[model] = entry
        except (ValueError, TypeError):
            pass
    # Fill in a default cache_read (0.1x input) wherever it's missing.
    for rates in pricing.values():
        rates.setdefault("cache_read", round(rates.get("input", 0) * 0.1, 6))
    return pricing


def set_pricing(pricing: dict[str, Any]) -> None:
    import json

    clean: dict[str, dict[str, float]] = {}
    for model, rates in (pricing or {}).items():
        if not isinstance(rates, dict):
            continue
        try:
            entry = {
                "input": float(rates.get("input", 0) or 0),
                "output": float(rates.get("output", 0) or 0),
            }
            if rates.get("cache_read") is not None:
                entry["cache_read"] = float(rates.get("cache_read") or 0)
            clean[str(model)] = entry
        except (ValueError, TypeError):
            continue
    set_setting("pricing", json.dumps(clean))


def estimate_cost(
    pricing: dict[str, dict[str, float]], model: str | None,
    input_tokens: int, output_tokens: int, cached_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """USD cost for a row given the pricing table (per 1M tokens).

    ``cached_tokens`` are billed at the (cheaper) ``cache_read`` rate. Whether
    they also count inside ``input_tokens`` depends on which upstream shape
    produced the row: OpenAI reports cache hits as a subset of prompt tokens,
    Anthropic reports them as a bucket of their own with ``input_tokens``
    holding only what missed the cache. The row itself does not say which, but
    a count larger than ``input_tokens`` cannot be a subset of it, so that case
    is treated as separate buckets. Ambiguous rows (cached <= input) keep the
    subset reading, which is right for OpenAI and understates an Anthropic row
    only when its cache hits were already small.

    ``cache_write_tokens`` are never part of ``input_tokens`` on either shape
    and are billed at the ``cache_write`` rate, defaulting to 1.25x input per
    Anthropic pricing.
    """
    rates = pricing.get(model or "") or pricing.get("default") or {}
    in_rate = rates.get("input", 0)
    cache_rate = rates.get("cache_read", in_rate * 0.1)
    write_rate = rates.get("cache_write", in_rate * 1.25)
    if (cached_tokens or 0) > (input_tokens or 0):
        cached = cached_tokens or 0
        uncached = input_tokens or 0
    else:
        cached = max(0, cached_tokens or 0)
        uncached = (input_tokens or 0) - cached
    cost_in = (uncached / 1_000_000) * in_rate
    cost_cache = (cached / 1_000_000) * cache_rate
    cost_write = ((cache_write_tokens or 0) / 1_000_000) * write_rate
    cost_out = (output_tokens / 1_000_000) * rates.get("output", 0)
    return cost_in + cost_cache + cost_write + cost_out


# Copilot reports billing as "nano AIU". Empirically 1e5 nano_aiu per token
# corresponds to $1 per 1M tokens, i.e. 1 USD == 1e11 nano_aiu. Cross-checked
# against published list prices: claude-opus-5 resolves to exactly $5/$25 and
# claude-sonnet-5 to its $2/$10 introductory rate, plus sonnet-4.5, haiku-4.5
# and gemini-3.5-flash.
NANO_AIU_PER_USD = 1e11


def row_equivalent_cost(
    pricing: dict[str, dict[str, float]], row: dict[str, Any]
) -> float:
    """Equivalent USD cost for an aggregate row, at vendor list prices.

    This is NOT the GitHub bill. A Copilot subscription is flat-rate; this
    figure answers "what would this traffic have cost at Anthropic / OpenAI /
    Google list prices", which is the only cost signal the upstream exposes.

    Rows split into two disjoint buckets that are summed, never double-counted:
    requests where Copilot reported an authoritative ``nano_aiu`` charge, and
    requests where it reported a zero rate -- those bill against the flat
    Copilot premium-request quota and fall back to the local pricing table.

    Which bucket a model lands in cannot be inferred from its vendor: among
    Azure OpenAI models, gpt-4o reports a zero rate while gpt-4 reports
    $2.50/$10. So the split is driven by nano_aiu itself, never by a name.

    A model with no pricing entry contributes nothing rather than falling back
    to the ``default`` rate. Quota-billed models are the overwhelming majority
    of the no-nano_aiu bucket, and pricing them at a generic $3/$15 would
    invent an equivalent cost for traffic that has no per-token price at all --
    they are already counted under ``quota_requests``.
    """
    cost = (row.get("nano_aiu") or 0) / NANO_AIU_PER_USD
    model = row.get("model")
    if model in pricing:
        cost += estimate_cost(
            pricing, model,
            row.get("fb_input_tokens", 0), row.get("fb_output_tokens", 0),
            row.get("fb_cached_tokens", 0), row.get("fb_cache_write_tokens", 0),
        )
    return cost


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
def create_api_key(name: str) -> dict[str, Any]:
    key = "sk-hub-" + secrets.token_urlsafe(32)
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO api_keys(key, name, created_at, revoked) VALUES(?,?,?,0)",
            (key, name, now),
        )
    return {"key": key, "name": name, "created_at": now, "revoked": False}


def list_api_keys() -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT key, name, created_at, revoked FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_api_key(key: str) -> None:
    with _conn() as c:
        c.execute("UPDATE api_keys SET revoked=1 WHERE key=?", (key,))


def is_valid_api_key(key: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM api_keys WHERE key=? AND revoked=0", (key,)
        ).fetchone()
        return row is not None


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #
def record_usage(
    *,
    api_key: str | None,
    model: str | None,
    served_model: str | None,
    endpoint: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    streamed: bool,
    estimated: bool,
    status: int = 200,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    nano_aiu: int = 0,
) -> None:
    with _conn() as c:
        c.execute(
            f"INSERT INTO {USAGE_TABLE}(ts, api_key, model, served_model, endpoint, "
            "input_tokens, output_tokens, total_tokens, cached_tokens, "
            "cache_write_tokens, nano_aiu, streamed, estimated, status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                api_key,
                model,
                served_model,
                endpoint,
                int(input_tokens or 0),
                int(output_tokens or 0),
                int(total_tokens or 0),
                int(cached_tokens or 0),
                int(cache_write_tokens or 0),
                int(nano_aiu or 0),
                1 if streamed else 0,
                1 if estimated else 0,
                int(status),
            ),
        )


def usage_summary(since: float | None, until: float | None) -> dict[str, Any]:
    where = []
    params: list[Any] = []
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    if until is not None:
        where.append("ts <= ?")
        params.append(until)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    # Same predicate but table-qualified, for queries that JOIN api_keys.
    uclause = clause.replace("ts ", "u.ts ") if clause else ""

    # Cost splits into two buckets that are summed but never overlap: rows with
    # an authoritative nano_aiu use it directly, rows the upstream priced at
    # zero fall back to the local pricing table.
    #
    # Because this reads usage_v2 only, nano_aiu = 0 has exactly one meaning --
    # the upstream priced the request at zero, i.e. a quota-billed model. (In
    # the legacy `usage` table it could also mean "not collected yet", which is
    # precisely why that table is not read here.) The one remaining exclusion
    # is failed requests: they carry nano_aiu = 0 but consumed no quota, so
    # status must be 2xx or every 400 would inflate the count.
    fb = (
        "COALESCE(SUM(nano_aiu),0) AS nano_aiu, "
        "COALESCE(SUM(CASE WHEN nano_aiu=0 AND status>=200 AND status<300 "
        "THEN 1 ELSE 0 END),0) AS quota_requests, "
        "COALESCE(SUM(CASE WHEN nano_aiu=0 THEN input_tokens ELSE 0 END),0) AS fb_input_tokens, "
        "COALESCE(SUM(CASE WHEN nano_aiu=0 THEN output_tokens ELSE 0 END),0) AS fb_output_tokens, "
        "COALESCE(SUM(CASE WHEN nano_aiu=0 THEN cached_tokens ELSE 0 END),0) AS fb_cached_tokens, "
        "COALESCE(SUM(CASE WHEN nano_aiu=0 THEN cache_write_tokens ELSE 0 END),0) AS fb_cache_write_tokens, "
    )
    ufb = (
        "COALESCE(SUM(u.nano_aiu),0) AS nano_aiu, "
        "COALESCE(SUM(CASE WHEN u.nano_aiu=0 AND u.status>=200 AND u.status<300 "
        "THEN 1 ELSE 0 END),0) AS quota_requests, "
        "COALESCE(SUM(CASE WHEN u.nano_aiu=0 THEN u.input_tokens ELSE 0 END),0) AS fb_input_tokens, "
        "COALESCE(SUM(CASE WHEN u.nano_aiu=0 THEN u.output_tokens ELSE 0 END),0) AS fb_output_tokens, "
        "COALESCE(SUM(CASE WHEN u.nano_aiu=0 THEN u.cached_tokens ELSE 0 END),0) AS fb_cached_tokens, "
        "COALESCE(SUM(CASE WHEN u.nano_aiu=0 THEN u.cache_write_tokens ELSE 0 END),0) AS fb_cache_write_tokens, "
    )

    with _conn() as c:
        totals = c.execute(
            f"SELECT COUNT(*) AS requests, "
            f"COALESCE(SUM(input_tokens),0) AS input_tokens, "
            f"COALESCE(SUM(output_tokens),0) AS output_tokens, "
            f"COALESCE(SUM(cached_tokens),0) AS cached_tokens, "
            f"COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens, "
            f"COALESCE(SUM(total_tokens),0) AS total_tokens "
            f"FROM {USAGE_TABLE} {clause}",
            params,
        ).fetchone()

        by_model = c.execute(
            f"SELECT model, COUNT(*) AS requests, "
            f"COALESCE(SUM(input_tokens),0) AS input_tokens, "
            f"COALESCE(SUM(output_tokens),0) AS output_tokens, "
            f"COALESCE(SUM(cached_tokens),0) AS cached_tokens, "
            f"COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens, "
            f"{fb}"
            f"COALESCE(SUM(total_tokens),0) AS total_tokens "
            f"FROM {USAGE_TABLE} {clause} GROUP BY model ORDER BY total_tokens DESC",
            params,
        ).fetchall()

        by_day = c.execute(
            f"SELECT date(ts, 'unixepoch', 'localtime') AS day, "
            f"COALESCE(SUM(total_tokens),0) AS total_tokens, "
            f"COUNT(*) AS requests "
            f"FROM {USAGE_TABLE} {clause} GROUP BY day ORDER BY day",
            params,
        ).fetchall()

        # Per-API-key token totals, joined to api_keys for a friendly name.
        by_api_key = c.execute(
            f"SELECT u.api_key AS key, "
            f"COALESCE(k.name, '') AS key_name, "
            f"COUNT(*) AS requests, "
            f"COALESCE(SUM(u.input_tokens),0) AS input_tokens, "
            f"COALESCE(SUM(u.output_tokens),0) AS output_tokens, "
            f"COALESCE(SUM(u.cached_tokens),0) AS cached_tokens, "
            f"COALESCE(SUM(u.cache_write_tokens),0) AS cache_write_tokens, "
            f"COALESCE(SUM(u.total_tokens),0) AS total_tokens "
            f"FROM {USAGE_TABLE} u LEFT JOIN api_keys k ON u.api_key = k.key {uclause} "
            f"GROUP BY u.api_key ORDER BY total_tokens DESC",
            params,
        ).fetchall()

        # (key, model) breakdown — used to compute per-key cost (cost is
        # per-model, but a key spans models). Not returned directly.
        by_key_model = c.execute(
            f"SELECT u.api_key AS key, u.model AS model, "
            f"{ufb}"
            f"COALESCE(SUM(u.input_tokens),0) AS input_tokens, "
            f"COALESCE(SUM(u.output_tokens),0) AS output_tokens, "
            f"COALESCE(SUM(u.cached_tokens),0) AS cached_tokens "
            f"FROM {USAGE_TABLE} u {uclause} GROUP BY u.api_key, u.model",
            params,
        ).fetchall()

    return {
        "totals": dict(totals),
        "by_model": [dict(r) for r in by_model],
        "by_day": [dict(r) for r in by_day],
        "by_api_key": [dict(r) for r in by_api_key],
        "_by_key_model": [dict(r) for r in by_key_model],
        "pricing": pricing_table(),
    }


def pricing_table() -> dict[str, dict[str, float]]:
    """Public alias kept stable for callers/tests."""
    return get_pricing()


def usage_summary_with_cost(since: float | None, until: float | None) -> dict[str, Any]:
    """usage_summary plus per-model, per-key and total cost figures.

    Two independent numbers are reported, deliberately not summed:

    ``equivalent_cost``  USD at vendor list prices, from Copilot's own
                         authoritative nano_aiu charge (plus the local pricing
                         table for models that report a zero rate).
    ``quota_requests``   Requests that billed against the flat Copilot
                         premium-request quota instead of a per-token rate.

    Adding these together would be meaningless -- one is money at a price list
    you do not actually pay, the other is a count against a subscription
    allowance -- so the UI shows them side by side.
    """
    data = usage_summary(since, until)
    pricing = data["pricing"]
    total_cost = 0.0
    total_quota = 0
    for row in data["by_model"]:
        cost = row_equivalent_cost(pricing, row)
        row["equivalent_cost"] = round(cost, 4)
        row["cost"] = row["equivalent_cost"]  # legacy alias
        total_cost += cost
        total_quota += int(row.get("quota_requests") or 0)
    data["totals"]["equivalent_cost"] = round(total_cost, 4)
    data["totals"]["cost"] = data["totals"]["equivalent_cost"]  # legacy alias
    data["totals"]["quota_requests"] = total_quota

    # Cost is per-model, so a key's total is the sum over its (key, model)
    # slices. Same for its quota-request count.
    cost_by_key: dict[Any, float] = {}
    quota_by_key: dict[Any, int] = {}
    for row in data.pop("_by_key_model", []):
        k = row.get("key")
        cost_by_key[k] = cost_by_key.get(k, 0.0) + row_equivalent_cost(pricing, row)
        quota_by_key[k] = quota_by_key.get(k, 0) + int(row.get("quota_requests") or 0)
    for row in data["by_api_key"]:
        k = row.get("key")
        row["equivalent_cost"] = round(cost_by_key.get(k, 0.0), 4)
        row["cost"] = row["equivalent_cost"]  # legacy alias
        row["quota_requests"] = quota_by_key.get(k, 0)

    return data


def recent_usage(limit: int = 50) -> list[dict[str, Any]]:
    """Recent rows, each with the key that made it and what it was worth.

    The key is joined rather than resolved in the caller so a deleted key still
    renders: the row keeps the key it was recorded with, and key_name simply
    comes back empty.

    ``equivalent_cost`` reuses the same per-row function the summary tables are
    built from, so a row's cost here and its contribution to the totals cannot
    drift apart. A row priced by the upstream uses that figure; one the
    upstream charged nothing for falls back to the local table, which is what
    the fb_* aliases below feed -- they carry this row's own tokens precisely
    because a single row is entirely in one bucket or the other.
    """
    pricing = get_pricing()
    with _conn() as c:
        rows = c.execute(
            "SELECT u.ts, u.api_key, COALESCE(k.name, '') AS key_name, "
            "u.model, u.served_model, u.endpoint, u.input_tokens, "
            "u.output_tokens, u.total_tokens, u.cached_tokens, "
            "u.cache_write_tokens, u.nano_aiu, u.streamed, u.estimated, u.status "
            f"FROM {USAGE_TABLE} u LEFT JOIN api_keys k ON u.api_key = k.key "
            "ORDER BY u.ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        priced = {
            **row,
            "fb_input_tokens": 0 if row["nano_aiu"] else row["input_tokens"],
            "fb_output_tokens": 0 if row["nano_aiu"] else row["output_tokens"],
            "fb_cached_tokens": 0 if row["nano_aiu"] else row["cached_tokens"],
            "fb_cache_write_tokens": 0 if row["nano_aiu"] else row["cache_write_tokens"],
        }
        row["equivalent_cost"] = row_equivalent_cost(pricing, priced)
        out.append(row)
    return out
