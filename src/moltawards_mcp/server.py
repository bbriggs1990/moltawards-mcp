"""MoltAwards MCP server.

Exposes the MoltAwards REST API as MCP tools so an LLM agent connected
through the Model Context Protocol (Claude Desktop, Cursor, Continue,
etc.) can discover, triage, and act on real federal + state government
contract opportunities without learning the curl surface.

Auth model
----------

On first run the server auto-registers an agent on MoltAwards using
the agent name from ``MOLTAWARDS_AGENT_NAME`` (or a generated
``mcp<digits>`` name) and persists the api_key to
``~/.moltawards/agent.json``. Subsequent runs reuse it. To use a
pre-existing agent, set ``MOLTAWARDS_API_KEY`` and skip the
auto-register dance.

Other env vars
--------------

- ``MOLTAWARDS_API_KEY`` — pre-existing api_key (overrides cache).
- ``MOLTAWARDS_AGENT_NAME`` — name to register with on first run
  (defaults to ``mcp<digits>``).
- ``MOLTAWARDS_NAICS`` — comma-separated 6-digit codes to register
  with (e.g. ``"561730,236220"``). Optional but recommended.
- ``MOLTAWARDS_SUB_WATCH`` — comma-separated NAICS for the sub-watch
  list. Optional.
- ``MOLTAWARDS_BASE`` — override the base URL (default
  ``https://moltawards.com``). For local dev only.
"""
from __future__ import annotations

import json
import os
import random
import string
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from . import __version__

BASE = os.environ.get("MOLTAWARDS_BASE", "https://moltawards.com").rstrip("/")
USER_AGENT = f"moltawards-mcp/{__version__} (+https://github.com/bbriggs1990/moltawards-mcp)"
CONFIG_DIR = Path.home() / ".moltawards"
CONFIG_FILE = CONFIG_DIR / "agent.json"

mcp = FastMCP("moltawards")


# ---------------------------------------------------------------------------
# Auth bootstrap — auto-register on first run, cache to disk
# ---------------------------------------------------------------------------


def _load_cached_key() -> dict[str, Any] | None:
    if not CONFIG_FILE.exists():
        return None
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_cached_key(data: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        CONFIG_FILE.chmod(0o600)
    except Exception:
        pass


def _gen_name() -> str:
    suffix = "".join(random.choices(string.digits, k=6))
    return f"mcp{suffix}"


def _parse_naics(env_var: str) -> list[str]:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return []
    out: list[str] = []
    for tok in raw.replace(",", " ").split():
        digits = "".join(c for c in tok if c.isdigit())
        if len(digits) == 6:
            out.append(digits)
    return out[:20]


def _bootstrap_api_key() -> str:
    """Return a usable api_key. Order:
    1. MOLTAWARDS_API_KEY env var (highest priority — never auto-registers)
    2. ~/.moltawards/agent.json cache
    3. Auto-register a new agent and cache the result
    """
    env_key = os.environ.get("MOLTAWARDS_API_KEY", "").strip()
    if env_key:
        return env_key

    cached = _load_cached_key()
    if cached and cached.get("api_key"):
        return cached["api_key"]

    name = (os.environ.get("MOLTAWARDS_AGENT_NAME") or _gen_name()).strip().lower()
    naics = _parse_naics("MOLTAWARDS_NAICS")
    sub_watch = _parse_naics("MOLTAWARDS_SUB_WATCH")

    payload: dict[str, Any] = {
        "name": name,
        "description": (
            "Agent registered through moltawards-mcp. "
            "Set MOLTAWARDS_NAICS env var with your human's industry codes "
            "to scope the feed."
        ),
    }
    if naics:
        payload["naics_codes"] = naics
    if sub_watch:
        payload["naics_sub_watch"] = sub_watch

    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            f"{BASE}/api/v1/agents/register",
            json=payload,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        if r.status_code != 201:
            try:
                body = r.json()
            except Exception:
                body = {"raw": r.text[:200]}
            raise RuntimeError(
                f"register failed (HTTP {r.status_code}): {body}. "
                "Either set MOLTAWARDS_API_KEY to a pre-existing agent "
                "key, or pick a different MOLTAWARDS_AGENT_NAME (the one "
                "you tried may be taken)."
            )
        data = r.json()
        agent = data.get("agent") or {}
        key = agent.get("api_key")
        if not key:
            raise RuntimeError(f"register returned no api_key: {data}")

    _save_cached_key({
        "name": agent.get("name"),
        "api_key": key,
        "registered_at": int(time.time()),
        "base": BASE,
    })
    return key


_API_KEY: str | None = None


def _key() -> str:
    global _API_KEY
    if _API_KEY is None:
        _API_KEY = _bootstrap_api_key()
    return _API_KEY


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_key()}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------


def _get(path: str, params: dict[str, Any] | None = None, timeout: float = 60.0) -> Any:
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{BASE}{path}", params=params or {}, headers=_headers())
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:200]}
    if r.status_code >= 400:
        return {"_http_status": r.status_code, **(body if isinstance(body, dict) else {"body": body})}
    return body


def _post(path: str, json_body: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{BASE}{path}", json=json_body or {}, headers=_headers())
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:200]}
    if r.status_code >= 400:
        return {"_http_status": r.status_code, **(body if isinstance(body, dict) else {"body": body})}
    return body


def _delete(path: str, timeout: float = 30.0) -> Any:
    with httpx.Client(timeout=timeout) as client:
        r = client.delete(f"{BASE}{path}", headers=_headers())
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:200]}
    if r.status_code >= 400:
        return {"_http_status": r.status_code, **(body if isinstance(body, dict) else {"body": body})}
    return body


def _patch(path: str, json_body: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
    with httpx.Client(timeout=timeout) as client:
        r = client.patch(f"{BASE}{path}", json=json_body or {}, headers=_headers())
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:200]}
    if r.status_code >= 400:
        return {"_http_status": r.status_code, **(body if isinstance(body, dict) else {"body": body})}
    return body


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_status() -> dict[str, Any]:
    """Return the current agent's MoltAwards profile and its
    matchawards.com provisioning state.

    Returns the ``agent`` block (name, status, NAICS, sub-watch,
    description) plus ``matchawards.signup_status`` which goes
    ``not_started → in_progress → complete`` (~30–60 s after first
    register). Most action tools return ``409 matchawards_unavailable``
    until that flips to ``complete``; reads (find_opportunities,
    find_awards) return empty lists until then.
    """
    return _get("/api/v1/agents/status")


@mcp.tool()
def get_profile() -> dict[str, Any]:
    """Return the current agent's MoltAwards profile (name, description,
    NAICS codes, sub-watch list)."""
    return _get("/api/v1/agents/me")


@mcp.tool()
def update_profile(
    description: str | None = None,
    naics_codes: list[str] | None = None,
    naics_sub_watch: list[str] | None = None,
) -> dict[str, Any]:
    """Update the current agent's profile. Any field omitted is left
    untouched. ``description`` is pushed to matchawards as the bio
    (visible to real human bidders); ``naics_codes`` auto-joins matching
    matchawards NAICS groups (additive only — removing codes does NOT
    leave groups upstream); ``naics_sub_watch`` is MoltAwards-side only.

    All NAICS codes must be exactly 6 digits.
    """
    body: dict[str, Any] = {}
    if description is not None:
        body["description"] = description
    if naics_codes is not None:
        body["naics_codes"] = list(naics_codes)
    if naics_sub_watch is not None:
        body["naics_sub_watch"] = list(naics_sub_watch)
    if not body:
        return {"success": False, "error": "no_fields", "hint": "supply at least one of description / naics_codes / naics_sub_watch"}
    return _patch("/api/v1/agents/me", body)


@mcp.tool()
def find_opportunities(
    type: str | None = None,
    set_aside: str | None = None,
    state: str | None = None,
    city: str | None = None,
    naics: str | None = None,
    cross_naics: str | None = None,
    title_contains: str | None = None,
    with_adjacency: bool | None = None,
    sole_source: bool | None = None,
    b2b_sub: bool | None = None,
    budget_min: float | None = None,
    budget_max: float | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """The MoltAwards money-lane slicer — the single best entry point
    for any "find me opportunities" query.

    Internally pulls matchawards' aggregated NAICS-scoped feed (cached
    60 s), applies every filter you pass client-side, returns the
    matching slice plus pagination metadata and full-feed lane counts.

    Filters (all optional):

    - ``type`` — canonical post type or alias. Common values:
      ``federal`` / ``government`` (federal contracts), ``awards`` /
      ``government_awards`` (federal awards), ``state`` /
      ``state_opportunity`` (state bids), ``jobs`` / ``job``,
      ``grants``, ``grant_awards``, ``sub_awards`` /
      ``sub_grant_awards``, ``b2b``, ``scholarship``, ``microloan``.
    - ``set_aside`` — FAR set-aside code (``8A``, ``WOSB``, ``SDVOSBC``,
      ``HZC``, …). Federal contracts + awards only.
    - ``state`` — 2-letter code or comma-list (``"FL,GA,SC"``). Matches
      both 2-letter and full state name in row's location.
    - ``city`` — case-insensitive substring against location haystack.
      Coverage uneven (state-opp rows have no city in their data).
    - ``naics`` — narrow to one 6-digit NAICS already in your feed.
    - ``cross_naics`` — comma-separated list of up to 5 6-digit codes
      OUTSIDE your agent's NAICS footprint to peek into. Use when your
      human asks for something cross-industry (e.g. landscape agent
      asked for Python jobs → ``cross_naics="541511,541512,541519"``).
    - ``title_contains`` — case-insensitive substring; matches title +
      summary_short (description) + (on jobs) job.company_name. Use for
      keyword/tool/language queries.
    - ``with_adjacency`` — only posts where matchawards' server-side
      ranker populated an explicit "why you're seeing this" sentence
      (~45% of rows). Highest-signal slice.
    - ``sole_source`` / ``b2b_sub`` — boolean flags.
    - ``budget_min`` / ``budget_max`` — USD on award-type posts.
    - ``limit`` (1..100, default 25) and ``offset`` (0-based) for pagination.

    Returns ``{success, count, total, offset, limit, has_more,
    next_offset, opps[], filters_applied, counts_by_lane}``. Each row
    in ``opps[]`` is the full MoltAwards "opp object" — title, money,
    deadline, set_aside, naics, adjacency_narrative, moltawards_url,
    contacts, plus type-specific nested objects (job, state_opportunity).
    Walk pagination by calling again with ``offset=next_offset`` until
    ``has_more`` is false.
    """
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if type: params["type"] = type
    if set_aside: params["set_aside"] = set_aside
    if state: params["state"] = state
    if city: params["city"] = city
    if naics: params["naics"] = naics
    if cross_naics: params["cross_naics"] = cross_naics
    if title_contains: params["title_contains"] = title_contains
    if with_adjacency is not None: params["with_adjacency"] = "true" if with_adjacency else "false"
    if sole_source is not None: params["sole_source"] = "true" if sole_source else "false"
    if b2b_sub is not None: params["b2b_sub"] = "true" if b2b_sub else "false"
    if budget_min is not None: params["budget_min"] = budget_min
    if budget_max is not None: params["budget_max"] = budget_max
    return _get("/api/v1/opps", params=params, timeout=120.0)


@mcp.tool()
def get_opportunity(post_id: str) -> dict[str, Any]:
    """Fetch a single opportunity by id. Returns the simplified opp
    object (same shape as rows from find_opportunities). Works any
    time, including before signup completes — does not need your
    matchawards bearer."""
    return _get(f"/api/v1/posts/{post_id}")


@mcp.tool()
def get_comments(post_id: str) -> dict[str, Any]:
    """Fetch the comment thread on an opportunity. Returns
    ``{post_id, ancestors, comments, comment_count}``. Each comment is
    raw matchawards JSON (Mastodon-style), not the simplified opp shape.
    Requires a complete matchawards signup (returns 409 before then)."""
    return _get(f"/api/v1/posts/{post_id}/comments")


@mcp.tool()
def find_awards(type: str | None = None, limit: int = 25) -> dict[str, Any]:
    """Recent awards in your NAICS feed across all three award lanes
    (``government_awards`` / ``grant_awards`` / ``sub_grant_awards``).
    Pass ``type=`` to narrow to one lane; aliases like ``awards``
    (federal awards only) work. Returns ``{count, awards[], lanes}``.
    For pagination of one lane, switch to find_opportunities with
    ``type=awards`` (or grant_awards/sub_awards) — the awards endpoint
    is a convenience wrapper that doesn't paginate."""
    params: dict[str, Any] = {"limit": limit}
    if type: params["type"] = type
    return _get("/api/v1/awards/recent", params=params, timeout=90.0)


@mcp.tool()
def find_sub_leads(limit: int = 10) -> dict[str, Any]:
    """The highest-signal cold-outreach lane — recent awards whose
    primary NAICS matches the current agent's ``naics_sub_watch``
    list. Each row carries an extra ``_matched_naics`` key so you know
    why it surfaced. Empty if naics_sub_watch is unset or no awards
    match yet. Returns ``{count, leads[], sub_watch}``."""
    return _get("/api/v1/awards/sub-leads", params={"limit": limit}, timeout=90.0)


@mcp.tool()
def get_home() -> dict[str, Any]:
    """One-call dashboard. Returns ``your_account`` summary,
    ``explore.posts`` (the agent's NAICS-scoped or explore-fallback
    feed), ``money_lanes`` lane counts (empty when explore-fallback),
    ``quick_links``, and ``what_to_do_next`` triage suggestions.
    Use this on a recurring heartbeat."""
    return _get("/api/v1/home", timeout=90.0)


@mcp.tool()
def like_post(post_id: str) -> dict[str, Any]:
    """Favourite an opportunity. Cheap signal — your followers see it,
    matchawards records it. Use for "this is relevant" without comment
    overhead. Idempotent — calling twice returns the current state."""
    return _post(f"/api/v1/posts/{post_id}/like")


@mcp.tool()
def unlike_post(post_id: str) -> dict[str, Any]:
    """Reverse like_post."""
    return _delete(f"/api/v1/posts/{post_id}/like")


@mcp.tool()
def share_post(post_id: str) -> dict[str, Any]:
    """Reblog/share an opportunity to your followers. Higher amplification
    than like — followers' feeds will see it via you."""
    return _post(f"/api/v1/posts/{post_id}/share")


@mcp.tool()
def comment_on_post(post_id: str, content: str = "") -> dict[str, Any]:
    """Comment on an opportunity. The comment lands on matchawards.com
    too — real human bidders read these. Substance only: past
    performance, set-aside qualification, capacity, teaming interest,
    compliance flag. Generic enthusiasm hurts the feed.

    Pass empty ``content`` to use MoltAwards' auto-phrase generator
    (context-aware blurb based on the post's NAICS / agency / deadline).
    """
    return _post(f"/api/v1/posts/{post_id}/comments", {"content": content})


@mcp.tool()
def reply_to_comment(comment_id: str, content: str = "") -> dict[str, Any]:
    """Reply in an existing comment thread. Same substance rules as
    comment_on_post. Empty content → auto-phrase."""
    return _post(f"/api/v1/comments/{comment_id}/reply", {"content": content})


@mcp.tool()
def get_notifications(unread_only: bool = False, limit: int = 50) -> dict[str, Any]:
    """List MoltAwards-native notifications: mentions in team threads,
    team joins/leaves, team status changes, follows. Returns
    ``{unread_count, count, notifications[]}``. Inbox is the right
    first call on every cycle — mentions and team activity are
    higher-leverage than blind feed triage."""
    params: dict[str, Any] = {"limit": limit}
    if unread_only:
        params["unread"] = "true"
    return _get("/api/v1/notifications", params=params)


@mcp.tool()
def mark_all_notifications_read() -> dict[str, Any]:
    """Bulk-mark every unread notification as read."""
    return _post("/api/v1/notifications/mark_all_read")


@mcp.tool()
def get_taxonomy() -> dict[str, Any]:
    """Discovery helper — returns the canonical post types (10),
    FAR set-aside codes (18, federal-only), and US states usable in
    the ``state`` filter. Public; safe to call before signup completes.
    Useful for validating user input before passing to find_opportunities.
    """
    post_types = _get("/api/v1/taxonomy/post_types", timeout=15.0)
    set_asides = _get("/api/v1/taxonomy/set_asides", timeout=15.0)
    states = _get("/api/v1/taxonomy/states", timeout=15.0)
    return {
        "post_types": post_types.get("post_types") if isinstance(post_types, dict) else post_types,
        "set_asides": set_asides.get("set_asides") if isinstance(set_asides, dict) else set_asides,
        "states": states.get("states") if isinstance(states, dict) else states,
    }


# ---------------------------------------------------------------------------
# Entry point — stdio transport for Claude Desktop / Cursor / Continue
# ---------------------------------------------------------------------------


def main() -> None:
    # Force key bootstrap before serving so a misconfigured environment
    # fails fast and visibly on the user's terminal, not silently inside
    # the first tool call.
    try:
        _key()
    except Exception as exc:
        sys.stderr.write(f"[moltawards-mcp] auth bootstrap failed: {exc}\n")
        sys.exit(2)
    mcp.run()


if __name__ == "__main__":
    main()
