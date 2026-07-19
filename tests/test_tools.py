"""Tool resolution must find tools in the interpreter's bin dir, not just PATH.

This is what lets jsm find ffsubsync/subscleaner when they were pip-installed
into the same private venv jsm runs from (the venv bin is not on $PATH).
"""

import os
import sys

from jsm import tools


def test_resolve_tool_prefers_path(monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda name: "/usr/bin/" + name)
    assert tools.resolve_tool("ffsubsync") == "/usr/bin/ffsubsync"
    assert tools.tool_available("ffsubsync") is True


def test_resolve_tool_falls_back_to_interpreter_bindir(tmp_path, monkeypatch):
    # Simulate a venv: interpreter in <venv>/bin, tool installed beside it.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_python = bindir / "python"
    fake_python.write_text("")
    tool = bindir / "subscleaner"
    tool.write_text("#!/bin/sh\n")
    os.chmod(tool, 0o755)

    monkeypatch.setattr(tools.shutil, "which", lambda name: None)  # not on PATH
    monkeypatch.setattr(tools.sys, "executable", str(fake_python))

    assert tools.resolve_tool("subscleaner") == str(tool)
    assert tools.tool_available("subscleaner") is True


def test_resolve_tool_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(tools.sys, "executable", str(tmp_path / "bin" / "python"))
    assert tools.resolve_tool("nope") is None
    assert tools.tool_available("nope") is False
