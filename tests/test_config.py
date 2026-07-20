import os

import pytest

from jsm.config import settings as settings_mod
from jsm.config.settings import (
    accounts_file,
    config_file,
    ensure_first_run_files,
    load_accounts,
    load_settings,
)


@pytest.fixture
def no_default_accounts(monkeypatch):
    """Isolate accounts.conf parsing from any shipped default account."""
    monkeypatch.setattr(settings_mod, "DEFAULT_ACCOUNTS", [])


def test_first_run_creates_template_without_credentials(no_default_accounts):
    ensure_first_run_files()
    text = accounts_file().read_text()
    assert "username;password" in text          # instructions present
    assert load_accounts() == []                # but no real accounts
    mode = os.stat(accounts_file()).st_mode & 0o777
    assert mode == 0o600


def test_accounts_parsing(no_default_accounts):
    ensure_first_run_files()
    accounts_file().write_text(
        "# comment\n\nalice;secret1\n  bob ; secret2 \nbroken-line\n;nouser\n"
    )
    assert load_accounts() == [("alice", "secret1"), ("bob", "secret2")]


def test_default_account_ships_and_user_accounts_add_on(monkeypatch):
    monkeypatch.setattr(settings_mod, "DEFAULT_ACCOUNTS", [("shipped", "pw0")])
    ensure_first_run_files()
    # No user file entries: the built-in default is available out of the box.
    accounts_file().write_text("# nothing here\n")
    assert load_accounts() == [("shipped", "pw0")]
    # User accounts are added on top of the default.
    accounts_file().write_text("mine;pw1\n")
    assert load_accounts() == [("shipped", "pw0"), ("mine", "pw1")]


def test_user_can_override_a_shipped_default(monkeypatch):
    monkeypatch.setattr(settings_mod, "DEFAULT_ACCOUNTS", [("shared", "old")])
    ensure_first_run_files()
    accounts_file().write_text("shared;mynewpassword\n")
    # Same username re-declared: the user's password wins, no duplicate row.
    assert load_accounts() == [("shared", "mynewpassword")]


def test_settings_defaults_and_load():
    settings = load_settings()
    # Default template: English primary, Swedish secondary, full pipeline on.
    assert settings.languages == ["en", "sv"]
    assert settings.primary_language == "en"
    assert settings.secondary_languages == ["sv"]
    assert settings.sync_by_default is True
    assert settings.clean_by_default is True
    assert settings.bulk_min_confidence == 0.99

    config_file().write_text(
        'libraries = ["/media"]\nlanguages = ["de", "en"]\nsync_by_default = false\n'
        'unknown_key = 1\n'
    )
    settings = load_settings()
    assert settings.libraries == ["/media"]
    assert settings.languages == ["de", "en"]
    assert settings.primary_language == "de"
    assert settings.sync_by_default is False


def test_settings_survive_broken_config():
    ensure_first_run_files()
    config_file().write_text("this is [not toml")
    settings = load_settings()
    assert settings.languages == ["en", "sv"]


def test_unified_base_dir(monkeypatch, tmp_path):
    from jsm.config.settings import base_dir, config_dir, data_dir, log_file

    monkeypatch.delenv("JSM_CONFIG_DIR", raising=False)
    monkeypatch.delenv("JSM_DATA_DIR", raising=False)
    monkeypatch.setenv("SYNCLAYER_HOME", str(tmp_path / "syn"))
    # Everything resolves inside the one folder.
    assert base_dir() == tmp_path / "syn"
    assert config_dir() == tmp_path / "syn"
    assert data_dir() == tmp_path / "syn"
    assert log_file() == tmp_path / "syn" / "logs" / "synclayer.log"


def test_dump_config_roundtrips():
    from jsm.config.settings import Settings, dump_config, save_settings

    original = Settings(
        libraries=["/media/movies", "/media/tv"],
        languages=["de", "en"],
        api_key="key with \"quotes\" and \\slash",
        sync_by_default=False,
        clean_by_default=True,
        bulk_min_confidence=0.9,
        queue_concurrency=3,
        subscleaner_path="/opt/x/subscleaner",
    )
    save_settings(original)
    reloaded = load_settings()
    assert reloaded.libraries == original.libraries
    assert reloaded.languages == ["de", "en"]
    assert reloaded.api_key == original.api_key           # escaping survived
    assert reloaded.sync_by_default is False
    assert reloaded.bulk_min_confidence == 0.9
    assert reloaded.queue_concurrency == 3
    assert reloaded.subscleaner_path == "/opt/x/subscleaner"
    # Output is valid TOML.
    import tomllib
    tomllib.loads(dump_config(original))
