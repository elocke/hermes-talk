"""Shared suite plumbing."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _ephemeral_runs_optin(monkeypatch):
    """The suite's EXPLICIT opt-in to non-durable run acceptance.

    The run-history tee is inert under pytest by design (see
    ``talk_runs._history_enabled``), and ``start_run`` refuses to accept work
    it cannot record durably unless the caller opts in by name — silence is
    exactly how a "durable" acceptance quietly stops being durable. Tests
    that exercise durability monkeypatch the tee ON (and this variable is
    then never consulted); the test asserting the refusal itself deletes it.
    """

    monkeypatch.setenv("TALK_RUNS_ALLOW_EPHEMERAL", "1")


@pytest.fixture(autouse=True)
def _no_extra_tools_from_the_box(monkeypatch):
    """The advertised tool list must not depend on the box running the suite.

    Desk tools and extra MCP tools ride on operator env; a developer's shell
    that has them set would otherwise change every tool-list assertion. Tests
    that exercise them set the variables explicitly.
    """

    import talk_mcp_tools

    for name in (
        "TALK_DESK_TOOLS",
        "TALK_DESK_MCP_URL",
        "TALK_DESK_MCP_KEY",
        "TALK_EXTRA_MCP_SERVER",
        "TALK_EXTRA_MCP_KEY_ENV",
        "TALK_EXTRA_MCP_TOOLS",
    ):
        monkeypatch.delenv(name, raising=False)
    talk_mcp_tools.reset()
    yield
    talk_mcp_tools.reset()
