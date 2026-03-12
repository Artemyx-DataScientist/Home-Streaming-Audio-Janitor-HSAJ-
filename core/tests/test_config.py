from __future__ import annotations

from pathlib import Path

import pytest

from hsaj.config import ConfigError, HsajConfig, LoadedConfig, load_config


def test_load_config_success(tmp_path: Path) -> None:
    config_path = tmp_path / "hsaj.yaml"
    config_path.write_text(
        """
database:
  driver: sqlite
  path: ./data/test.db
paths:
  library_roots:
    - ./music
  scan_exclude_dirs:
    - ./music/_tmp
  scan_batch_size: 50
policy:
  block_grace_days: 14
  quarantine_delete_days: 90
  auto_delete: true
"""
    )

    loaded = load_config(config_path)

    assert isinstance(loaded, LoadedConfig)
    assert isinstance(loaded.config, HsajConfig)
    assert loaded.config.database.path == (config_path.parent / "data/test.db").resolve()
    assert loaded.config.paths.library_roots == [(config_path.parent / "music").resolve()]
    assert loaded.config.paths.scan_exclude_dirs == [(config_path.parent / "music/_tmp").resolve()]
    assert loaded.config.paths.scan_batch_size == 50
    assert loaded.config.policy.block_grace_days == 14
    assert loaded.config.policy.quarantine_delete_days == 90
    assert loaded.config.policy.auto_delete is True


def test_load_config_empty_file(tmp_path: Path) -> None:
    config_path = tmp_path / "hsaj.yaml"
    config_path.write_text("")

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_config_unknown_driver(tmp_path: Path) -> None:
    config_path = tmp_path / "hsaj.yaml"
    config_path.write_text(
        """
database:
  driver: postgres
  path: ./data/test.db
"""
    )

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_config_applies_operator_token_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "hsaj.yaml"
    config_path.write_text(
        """
database:
  driver: sqlite
  path: ./data/test.db
"""
    )
    monkeypatch.setenv("HSAJ_OPERATOR_TOKEN", "top-secret")

    loaded = load_config(config_path)

    assert loaded.config.security.operator_token == "top-secret"


def test_load_config_runtime_section(tmp_path: Path) -> None:
    config_path = tmp_path / "hsaj.yaml"
    config_path.write_text(
        """
database:
  driver: sqlite
  path: ./data/test.db
runtime:
  enable_background_jobs: true
  blocked_sync_interval_minutes: 5
  cleanup_interval_minutes: 30
  blocked_sync_on_start: false
  cleanup_on_start: true
"""
    )

    loaded = load_config(config_path)

    assert loaded.config.runtime.enable_background_jobs is True
    assert loaded.config.runtime.blocked_sync_interval_minutes == 5
    assert loaded.config.runtime.cleanup_interval_minutes == 30
    assert loaded.config.runtime.blocked_sync_on_start is False
    assert loaded.config.runtime.cleanup_on_start is True
