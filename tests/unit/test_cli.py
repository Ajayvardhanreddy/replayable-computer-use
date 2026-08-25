"""CLI wiring smoke tests (no browser, no model)."""

from typer.testing import CliRunner

from computer_use.cli import app

runner = CliRunner()


def test_handoff_demo_command_is_registered() -> None:
    result = runner.invoke(app, ["handoff-demo", "--help"])
    assert result.exit_code == 0
    assert "hand the live session" in result.output
    assert "--scenario" in result.output
    assert "--headed" in result.output


def test_top_level_help_lists_core_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("discover", "replay", "handoff-demo"):
        assert command in result.output
