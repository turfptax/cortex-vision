"""Regression tests for the __main__.py CLI dispatcher.

The dispatcher has two invocation patterns we care about:

  1. `cortex-vision.exe`              (no args, double-click / smoke test)
  2. `cortex-vision.exe serve ...`     (subcommand, cortex-desktop's call)

Both must reach `cortex_vision.server.main()`. The bug fixed here was that
pattern 1 hit AttributeError on `args.host` because argparse subparser
attributes only exist when the subcommand is explicitly provided.
"""
from __future__ import annotations

import sys
from unittest.mock import patch

import pytest


def test_main_no_args_does_not_attribute_error(monkeypatch):
    """Regression: bundling __main__.py and running cortex-vision.exe with
    no args should default to `serve` without crashing on missing attrs."""
    monkeypatch.setattr(sys, "argv", ["cortex-vision"])

    called = {}

    def fake_serve_main():
        called["host"] = sys.argv
        # Don't actually start uvicorn — just return

    with patch("cortex_vision.server.main", fake_serve_main):
        from cortex_vision.__main__ import main
        main()

    assert called, "serve_main() was not reached"


def test_main_serve_subcommand_passes_through_flags(monkeypatch):
    monkeypatch.setattr(
        sys, "argv",
        ["cortex-vision", "serve", "--port", "9999", "--host", "0.0.0.0"],
    )

    captured: list[str] = []

    def fake_serve_main():
        captured.extend(sys.argv)

    with patch("cortex_vision.server.main", fake_serve_main):
        from cortex_vision.__main__ import main
        main()

    assert "--port" in captured
    assert "9999" in captured
    assert "--host" in captured
    assert "0.0.0.0" in captured


def test_main_version_subcommand(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cortex-vision", "version"])
    from cortex_vision.__main__ import main
    main()
    out = capsys.readouterr().out.strip()
    # Should print something that looks like a version
    assert "." in out
    assert len(out) <= 20
