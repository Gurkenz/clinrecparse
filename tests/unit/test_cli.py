from pathlib import Path

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
    assert "research-validate-corpus" in result.output
    assert "research-migrate-layout" in result.output
    assert "research-profile-corpus" in result.output


def test_identity_conflict_bypass_flags_are_not_exposed() -> None:
    apply_help = runner.invoke(app, ["bank-apply-update", "--help"])
    stage_help = runner.invoke(app, ["bank-stage-update", "--help"])

    assert apply_help.exit_code == 0
    assert stage_help.exit_code == 0
    assert "--allow-identity-conflict" not in apply_help.output
    assert "--allow-identity-conflict" not in stage_help.output


def test_qa_command_with_empty_temp_data(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    data_root = tmp_path / "data"
    config_path.write_text(
        f"""
paths:
  data_root: {data_root.as_posix()}
  snapshots: {(data_root / "snapshots").as_posix()}
  references: {(data_root / "references").as_posix()}
  documents: {(data_root / "documents").as_posix()}
  indexes: {(data_root / "indexes").as_posix()}
  reports: {(data_root / "reports").as_posix()}
  logs: {(data_root / "logs").as_posix()}
http:
  timeout_seconds: 5
  retries: 0
  backoff_initial_seconds: 0.01
  backoff_max_seconds: 0.01
rate_limit:
  requests_per_second: 2
concurrency:
  default: 1
  max: 2
discovery:
  unavailable_retry_ttl_days: 7
logging:
  level: INFO
  jsonl_path: {(data_root / "logs" / "test.jsonl").as_posix()}
""".lstrip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["qa", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "qa completed" in result.output
