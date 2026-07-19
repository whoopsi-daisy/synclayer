import os

from jsm.config.settings import (
    accounts_file,
    config_file,
    ensure_first_run_files,
    load_accounts,
    load_settings,
)


def test_first_run_creates_template_without_credentials():
    ensure_first_run_files()
    text = accounts_file().read_text()
    assert "username;password" in text          # instructions present
    assert load_accounts() == []                # but no real accounts
    mode = os.stat(accounts_file()).st_mode & 0o777
    assert mode == 0o600


def test_accounts_parsing():
    ensure_first_run_files()
    accounts_file().write_text(
        "# comment\n\nalice;secret1\n  bob ; secret2 \nbroken-line\n;nouser\n"
    )
    assert load_accounts() == [("alice", "secret1"), ("bob", "secret2")]


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
