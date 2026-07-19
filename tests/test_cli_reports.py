import json

from jsm import main as cli
from jsm.config.settings import config_file, ensure_first_run_files
from jsm.reports import format_report, missing_report


def write_config(media_tree):
    ensure_first_run_files()
    config_file().write_text(
        f'libraries = ["{media_tree}"]\nlanguages = ["en"]\n'
    )


def test_scan_and_missing_report(media_tree, capsys):
    write_config(media_tree)
    assert cli.main(["scan"]) == 0
    out = capsys.readouterr().out
    assert "4 file(s)" in out

    assert cli.main(["missing"]) == 0
    out = capsys.readouterr().out
    assert "Winnie The Pooh (2011).mkv" in out
    assert "DuckTales (1990).mkv" in out          # wrong language
    assert "Alien (1979).mkv" not in out          # has English sub


def test_missing_report_formats(db, scanner, media_tree):
    scanner.scan(media_tree)
    rows = missing_report(db)
    assert {r["status"] for r in rows} == {"missing", "wrong_lang"}

    parsed = json.loads(format_report(rows, "json"))
    assert len(parsed) == len(rows)

    csv_text = format_report(rows, "csv")
    assert csv_text.splitlines()[0] == "path,status,existing_languages,size"

    text = format_report(rows, "text")
    assert "need attention" in text
    assert format_report([], "text").startswith("All good")


def test_missing_report_to_file(media_tree, tmp_path, capsys):
    write_config(media_tree)
    cli.main(["scan"])
    capsys.readouterr()
    target = tmp_path / "report.csv"
    assert cli.main(["missing", "--format", "csv", "-o", str(target)]) == 0
    assert target.read_text().startswith("path,")


def test_download_requires_paths_or_all(media_tree, capsys):
    write_config(media_tree)
    assert cli.main(["download"]) == 2


def test_bulk_download_aborts_without_confirmation(media_tree, capsys, monkeypatch):
    write_config(media_tree)
    cli.main(["scan"])
    monkeypatch.setattr("builtins.input", lambda prompt="": "no thanks")
    assert cli.main(["download", "--all"]) == 1
    out = capsys.readouterr().out
    assert "Aborted" in out


def test_bulk_dry_run_needs_no_confirmation(media_tree, capsys, monkeypatch):
    write_config(media_tree)
    cli.main(["scan"])

    def boom(prompt=""):
        raise AssertionError("dry-run must not prompt")

    monkeypatch.setattr("builtins.input", boom)
    # dry-run searches via the real provider which has no API key -> it reports
    # the failure per file but exits without writing anything
    rc = cli.main(["download", "--all", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "API key" in out
