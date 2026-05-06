"""Tests for the whisper.cpp local-binary transcription path (v0.3.5).

cortex-vision detects cortex-desktop's whisper.cpp install at
%APPDATA%/Cortex/whisper-cpp/ and uses it directly via subprocess. This
gives users free local transcription without configuring anything in
cortex-vision — they just need cortex-desktop's overseer to have set up
whisper.cpp once (which it does for the voice journal feature).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Each test gets fresh fake APPDATA / Program Files / etc. so we
    never see the dev's real cortex-desktop install during testing."""
    fake_appdata = tmp_path / "AppData" / "Roaming"
    fake_appdata.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(fake_appdata))
    # Wipe ALL paths v0.4.0's find_whisper_cli() searches.
    # (Program Files / LocalAppData / shutil.which fallback)
    for var in (
        "CORTEX_VISION_WHISPER_CLI",
        "CORTEX_VISION_WHISPER_URL",
        "CORTEX_VISION_WHISPER_KEY",
        "CORTEX_VISION_WHISPER_MODEL",
        "OPENAI_API_KEY",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "LOCALAPPDATA",
    ):
        monkeypatch.delenv(var, raising=False)
    # Block shutil.which from finding whisper-cli on the real PATH
    monkeypatch.setattr("shutil.which", lambda name: None)
    # Point the config file path at the fake location too
    monkeypatch.setattr(
        "cortex_vision.storage.db.default_db_path",
        lambda: tmp_path / "video" / "sessions.db",
    )
    yield fake_appdata


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def test_find_whisper_cli_returns_none_when_missing(_isolate_env):
    from cortex_vision.audio.transcribe import find_whisper_cli
    assert find_whisper_cli() is None


def test_find_whisper_cli_returns_path_when_present(_isolate_env):
    from cortex_vision.audio.transcribe import find_whisper_cli

    cli_dir = _isolate_env / "Cortex" / "whisper-cpp"
    cli_dir.mkdir(parents=True)
    cli = cli_dir / "whisper-cli.exe"
    cli.write_bytes(b"#!fake whisper-cli")

    assert find_whisper_cli() == cli


def test_find_whisper_cli_finds_cortex_desktop_install(monkeypatch, tmp_path):
    """v0.4.0 fix: detect whisper-cli inside cortex-desktop's PyInstaller
    install at <ProgramFiles>/CortexHub/_internal/backend/bin/whisper-cli.exe.
    This is where the official cortex-desktop installer drops it."""
    from cortex_vision.audio.transcribe import find_whisper_cli

    program_files = tmp_path / "Program Files"
    program_files.mkdir()
    monkeypatch.setenv("ProgramFiles", str(program_files))

    bundle_bin = program_files / "CortexHub" / "_internal" / "backend" / "bin"
    bundle_bin.mkdir(parents=True)
    cli = bundle_bin / "whisper-cli.exe"
    cli.write_bytes(b"#!fake")

    # Wipe other env vars that could match
    monkeypatch.delenv("CORTEX_VISION_WHISPER_CLI", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)

    assert find_whisper_cli() == cli


def test_find_whisper_cli_finds_program_files_x86(monkeypatch, tmp_path):
    """The user's actual install location: ProgramFiles(x86)/CortexHub/..."""
    from cortex_vision.audio.transcribe import find_whisper_cli

    pf86 = tmp_path / "ProgramFilesx86"
    pf86.mkdir()
    monkeypatch.setenv("ProgramFiles(x86)", str(pf86))

    cli = pf86 / "CortexHub" / "_internal" / "backend" / "bin" / "whisper-cli.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"#!fake")

    monkeypatch.delenv("CORTEX_VISION_WHISPER_CLI", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)

    assert find_whisper_cli() == cli


def test_find_whisper_cli_env_override_wins(monkeypatch, tmp_path):
    """CORTEX_VISION_WHISPER_CLI takes precedence over all other paths."""
    from cortex_vision.audio.transcribe import find_whisper_cli

    explicit = tmp_path / "my-custom-whisper" / "whisper-cli.exe"
    explicit.parent.mkdir(parents=True)
    explicit.write_bytes(b"#!explicit")
    monkeypatch.setenv("CORTEX_VISION_WHISPER_CLI", str(explicit))

    # Also set up a CortexHub install — should be ignored in favor of env
    pf = tmp_path / "ProgramFiles"
    pf.mkdir()
    bundle = pf / "CortexHub" / "_internal" / "backend" / "bin" / "whisper-cli.exe"
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b"#!ignored")
    monkeypatch.setenv("ProgramFiles", str(pf))

    assert find_whisper_cli() == explicit


def test_find_whisper_cli_handles_missing_appdata(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    from cortex_vision.audio.transcribe import find_whisper_cli
    assert find_whisper_cli() is None


def test_find_whisper_model_prefers_larger(_isolate_env):
    from cortex_vision.audio.transcribe import find_whisper_model

    models_dir = _isolate_env / "Cortex" / "whisper-models"
    models_dir.mkdir(parents=True)

    # Drop a base AND a large model in there
    (models_dir / "ggml-base.bin").write_bytes(b"small")
    (models_dir / "ggml-large-v3.bin").write_bytes(b"big")

    found = find_whisper_model()
    assert found is not None
    assert found.name == "ggml-large-v3.bin"


def test_find_whisper_model_falls_back_to_any_ggml(_isolate_env):
    """Non-canonical model name still gets picked up."""
    from cortex_vision.audio.transcribe import find_whisper_model

    models_dir = _isolate_env / "Cortex" / "whisper-models"
    models_dir.mkdir(parents=True)
    (models_dir / "ggml-custom-finetune.bin").write_bytes(b"custom")

    assert find_whisper_model() is not None


def test_find_whisper_model_returns_none_when_dir_empty(_isolate_env):
    from cortex_vision.audio.transcribe import find_whisper_model

    models_dir = _isolate_env / "Cortex" / "whisper-models"
    models_dir.mkdir(parents=True)
    assert find_whisper_model() is None


# ---------------------------------------------------------------------------
# Resolution priority
# ---------------------------------------------------------------------------

def test_resolution_priority_explicit_url_beats_whisper_cpp(_isolate_env, monkeypatch):
    """Even if whisper.cpp is installed, explicit CORTEX_VISION_WHISPER_URL wins."""
    from cortex_vision.audio.transcribe import _resolve_endpoint, _HttpEndpoint

    # Set up whisper.cpp install
    cli_dir = _isolate_env / "Cortex" / "whisper-cpp"
    cli_dir.mkdir(parents=True)
    (cli_dir / "whisper-cli.exe").write_bytes(b"x")
    models_dir = _isolate_env / "Cortex" / "whisper-models"
    models_dir.mkdir(parents=True)
    (models_dir / "ggml-large-v3.bin").write_bytes(b"x")

    # Also set explicit URL
    monkeypatch.setenv("CORTEX_VISION_WHISPER_URL", "http://lmstudio:1234/v1")

    endpoint = _resolve_endpoint()
    assert isinstance(endpoint, _HttpEndpoint)
    assert endpoint.name == "lmstudio_compat"


def test_resolution_priority_whisper_cpp_beats_openai(_isolate_env, monkeypatch):
    """When no explicit URL, whisper.cpp is preferred over OpenAI cloud."""
    from cortex_vision.audio.transcribe import _resolve_endpoint, _LocalWhisperCpp

    cli_dir = _isolate_env / "Cortex" / "whisper-cpp"
    cli_dir.mkdir(parents=True)
    (cli_dir / "whisper-cli.exe").write_bytes(b"x")
    models_dir = _isolate_env / "Cortex" / "whisper-models"
    models_dir.mkdir(parents=True)
    (models_dir / "ggml-large-v3.bin").write_bytes(b"x")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

    endpoint = _resolve_endpoint()
    assert isinstance(endpoint, _LocalWhisperCpp)
    assert endpoint.cli_path.name == "whisper-cli.exe"
    assert endpoint.model_path.name == "ggml-large-v3.bin"


def test_resolution_falls_back_to_openai(_isolate_env, monkeypatch):
    """No URL config and no whisper.cpp install — OpenAI key wins by default."""
    from cortex_vision.audio.transcribe import _resolve_endpoint, _HttpEndpoint

    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

    endpoint = _resolve_endpoint()
    assert isinstance(endpoint, _HttpEndpoint)
    assert endpoint.name == "openai"


def test_resolution_raises_when_no_provider(_isolate_env):
    from cortex_vision.audio.transcribe import _resolve_endpoint, WhisperUnavailable

    with pytest.raises(WhisperUnavailable):
        _resolve_endpoint()


# ---------------------------------------------------------------------------
# Subprocess invocation
# ---------------------------------------------------------------------------

def test_transcribe_via_whisper_cpp_parses_output(_isolate_env, monkeypatch, tmp_path):
    """The end-to-end whisper.cpp path: subprocess.run is mocked to write
    the JSON file the way whisper-cli would, then we parse it."""
    from cortex_vision.audio.transcribe import (
        _LocalWhisperCpp,
        _transcribe_via_whisper_cpp,
    )

    # Set up the install dirs (so we can construct a valid endpoint)
    cli_dir = _isolate_env / "Cortex" / "whisper-cpp"
    cli_dir.mkdir(parents=True)
    cli = cli_dir / "whisper-cli.exe"
    cli.write_bytes(b"x")
    models_dir = _isolate_env / "Cortex" / "whisper-models"
    models_dir.mkdir(parents=True)
    model = models_dir / "ggml-large-v3.bin"
    model.write_bytes(b"x")

    endpoint = _LocalWhisperCpp(cli_path=cli, model_path=model)

    # Fake input WAV
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 100)

    # whisper-cli -oj writes <input>.json with this shape:
    fake_payload = {
        "transcription": [
            {
                "timestamps": {"from": "00:00:00,000", "to": "00:00:01,500"},
                "offsets": {"from": 0, "to": 1500},
                "text": " hello world",
            },
            {
                "timestamps": {"from": "00:00:01,500", "to": "00:00:03,000"},
                "offsets": {"from": 1500, "to": 3000},
                "text": " how are you",
            },
        ]
    }

    def fake_run(cmd, **kw):
        # whisper-cli writes to <output_base>.json — find that path in cmd
        of_idx = cmd.index("-of")
        output_base = Path(cmd[of_idx + 1])
        output_json = output_base.with_suffix(".json")
        output_json.write_text(json.dumps(fake_payload), encoding="utf-8")

        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    with patch("subprocess.run", fake_run):
        result = _transcribe_via_whisper_cpp(endpoint, wav, language=None, timeout=30.0)

    assert result.provider == "whisper_cpp"
    assert result.model == "ggml-large-v3.bin"
    assert result.full_text == "hello world how are you"
    assert len(result.segments) == 2
    assert result.segments[0].start_s == 0.0
    assert result.segments[0].end_s == 1.5
    assert result.segments[0].text == "hello world"
    assert result.segments[1].start_s == 1.5
    assert result.segments[1].end_s == 3.0


def test_transcribe_via_whisper_cpp_handles_subprocess_failure(_isolate_env, tmp_path):
    """If whisper-cli exits non-zero, we raise WhisperUnavailable cleanly."""
    from cortex_vision.audio.transcribe import (
        _LocalWhisperCpp,
        _transcribe_via_whisper_cpp,
        WhisperUnavailable,
    )

    cli = _isolate_env / "Cortex" / "whisper-cpp" / "whisper-cli.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"x")
    model = _isolate_env / "Cortex" / "whisper-models" / "ggml-base.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"x")

    endpoint = _LocalWhisperCpp(cli_path=cli, model_path=model)
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"RIFF")

    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stderr = "model file corrupt"

    with patch("subprocess.run", lambda *a, **kw: fake_result), \
         pytest.raises(WhisperUnavailable, match="exit 1"):
        _transcribe_via_whisper_cpp(endpoint, wav, language=None, timeout=30.0)


def test_transcribe_via_whisper_cpp_handles_no_output_json(_isolate_env, tmp_path):
    """If whisper-cli succeeds but produces no JSON (rare), we surface it."""
    from cortex_vision.audio.transcribe import (
        _LocalWhisperCpp,
        _transcribe_via_whisper_cpp,
        WhisperUnavailable,
    )

    cli = _isolate_env / "Cortex" / "whisper-cpp" / "whisper-cli.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"x")
    model = _isolate_env / "Cortex" / "whisper-models" / "ggml-base.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"x")

    endpoint = _LocalWhisperCpp(cli_path=cli, model_path=model)
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"RIFF")

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stderr = ""

    with patch("subprocess.run", lambda *a, **kw: fake_result), \
         pytest.raises(WhisperUnavailable, match="produced no output"):
        _transcribe_via_whisper_cpp(endpoint, wav, language=None, timeout=30.0)


# ---------------------------------------------------------------------------
# Diagnostics integration
# ---------------------------------------------------------------------------

def test_active_provider_info_reports_whisper_cpp(_isolate_env):
    """When whisper.cpp is the active provider, diagnostics shows the local paths."""
    from cortex_vision.audio.transcribe import active_provider_info

    cli = _isolate_env / "Cortex" / "whisper-cpp" / "whisper-cli.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"x")
    model = _isolate_env / "Cortex" / "whisper-models" / "ggml-large-v3.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"x")

    info = active_provider_info()
    assert info["configured"] is True
    assert info["provider"] == "whisper_cpp"
    assert info["cli_path"].endswith("whisper-cli.exe")
    assert info["model"] == "ggml-large-v3.bin"


def test_active_provider_info_reports_unconfigured(_isolate_env):
    """No provider available → configured: false."""
    from cortex_vision.audio.transcribe import active_provider_info

    info = active_provider_info()
    assert info["configured"] is False
    assert info["provider"] is None
