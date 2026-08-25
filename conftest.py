"""Session-wide pytest configuration.

The ``live`` marker guards the single test that makes a genuine model API call.
It is opt-in via ``--run-live`` and stays skipped for an ordinary ``pytest`` run
even when ``ANTHROPIC_API_KEY`` happens to be exported, so a normal local or CI
run never spends model credits by accident.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    # Load a local, git-ignored .env so an opt-in --run-live run can pick up
    # ANTHROPIC_API_KEY without it being exported in the shell. No-op if absent.
    from computer_use.cli import _load_dotenv

    _load_dotenv()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run @pytest.mark.live tests (genuine model API calls; costs credits).",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="live test; pass --run-live to run (opt-in)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
