from typer.testing import CliRunner

from clinrec import __version__
from clinrec.cli import app

runner = CliRunner()


def test_package_imports() -> None:
    assert __version__ == "0.1.0"


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "sync-catalog" in result.output
    assert "run-all" in result.output


def test_qa_smoke_command() -> None:
    result = runner.invoke(app, ["qa"])

    assert result.exit_code == 0
    assert "qa: config loaded" in result.output
