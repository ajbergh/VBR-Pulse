"""The `pulse` command line: init, config, secrets, serve safeguards."""

from __future__ import annotations

from pathlib import Path

import pytest

from pulse import __version__, cli, config


def run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    return int(exit_info.value.code or 0)


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"VBR Pulse {__version__}"


def test_init_writes_the_template_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "pulse.env"
    assert run(["init", "--config", str(target)]) == 0
    assert target.read_text(encoding="utf-8") == config.CONFIG_TEMPLATE
    assert "pulse secret set" in capsys.readouterr().out
    assert run(["init", "--config", str(target)]) == 1  # never overwrites by accident
    assert run(["init", "--config", str(target), "--force"]) == 0


def test_config_shows_missing_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "_keyring_get", lambda service, user: None)
    for key in ("PULSE_PROFILE_OPS_USER", "PULSE_PROFILE_OPS_SECRET", "PULSE_PROFILES"):
        monkeypatch.delenv(key, raising=False)
    target = tmp_path / "pulse.env"
    target.write_text("PULSE_PROFILES=ops\nPULSE_PROFILE_OPS_USER=alice\n", encoding="utf-8")
    assert run(["config", "--config", str(target)]) == 0
    out = capsys.readouterr().out
    assert str(target) in out
    assert "alice" in out and "NO PASSWORD" in out


def test_secret_set_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, str] = {}
    monkeypatch.setattr(cli, "store_secret", lambda user, secret: store.__setitem__(user, secret))
    monkeypatch.setattr(cli, "delete_secret", lambda user: store.pop(user, None) is not None)
    answers = iter(["s3cret", "s3cret"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(answers))
    assert run(["secret", "set", "alice"]) == 0
    assert store == {"alice": "s3cret"}
    assert run(["secret", "delete", "alice"]) == 0
    assert run(["secret", "delete", "alice"]) == 1


def test_secret_mismatch_stores_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    stored: list[str] = []
    monkeypatch.setattr(cli, "store_secret", lambda user, secret: stored.append(user))
    answers = iter(["one", "two"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(answers))
    assert run(["secret", "set", "alice"]) == 1
    assert stored == []


def test_busy_port_falls_back_unless_chosen(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_port_free", lambda host, port: False)
    settings = config.Settings(_env_file=None)  # type: ignore[call-arg]
    assert cli._serve(settings, "127.0.0.1", 8000, False, port_explicit=True) == 2
    assert "already in use" in capsys.readouterr().err
