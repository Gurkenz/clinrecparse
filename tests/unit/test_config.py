from pathlib import Path

from clinrec.config import load_settings


def test_load_default_config() -> None:
    settings = load_settings(Path("config/default.yaml"))

    assert settings.paths.data_root == Path("data")
    assert settings.http.timeout_seconds == 30.0
    assert settings.rate_limit.requests_per_second == 2.0
    assert settings.concurrency.default == 1
    assert settings.concurrency.max == 2
