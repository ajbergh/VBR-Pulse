"""Profile + secret resolution: OS keyring → environment → vault reference (PLAN §8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pulse import config
from pulse.config import Profile, ProfileError, Settings, resolve_secret


@pytest.fixture
def keyring_store(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(config, "_keyring_get", lambda s, u: store.get((s, u)))
    return store


def test_keyring_wins(keyring_store: dict[tuple[str, str], str]) -> None:
    keyring_store[("vbr-pulse", "svc-pulse-ops")] = "from-keyring"
    profile = Profile("ops", "svc-pulse-ops", "from-env")
    assert resolve_secret(profile).get_secret_value() == "from-keyring"


def test_env_literal(keyring_store: dict[tuple[str, str], str]) -> None:
    profile = Profile("ops", "svc-pulse-ops", "from-env")
    assert resolve_secret(profile).get_secret_value() == "from-env"


def test_vault_reference(keyring_store: dict[tuple[str, str], str]) -> None:
    keyring_store[("vbr", "svc-pulse-ir")] = "from-vault"
    profile = Profile("ir", "svc-pulse-ir", "vault://vbr/svc-pulse-ir")
    assert resolve_secret(profile).get_secret_value() == "from-vault"


def test_missing_secret_message_has_no_value(keyring_store: dict[tuple[str, str], str]) -> None:
    profile = Profile("view", "svc-pulse-view", "vault://vbr/svc-pulse-view")
    with pytest.raises(ProfileError, match="pulse secret set svc-pulse-view"):
        resolve_secret(profile)


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_VBR_URL", "https://vbr01.lab.local")
    monkeypatch.setenv("PULSE_PROFILES", "ops, view")
    monkeypatch.setenv("PULSE_PROFILE_VIEW_USER", "svc-pulse-view")
    monkeypatch.setenv("PULSE_CA_BUNDLE", "")
    monkeypatch.setenv("MOCK", "1")

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.vbr_url == "https://vbr01.lab.local"
    assert s.profile_names == ["ops", "view"]
    assert s.profile("view").username == "svc-pulse-view"
    assert s.tls_verify is True
    assert s.mode == "mock"
    assert s.api_version == "1.3-rev2"


def test_insecure_tls_disables_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_LAB_INSECURE_TLS", "true")
    monkeypatch.setenv("PULSE_CA_BUNDLE", "./certs/ca.pem")
    assert Settings(_env_file=None).tls_verify is False  # type: ignore[call-arg]


def test_ca_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_CA_BUNDLE", "./certs/ca.pem")
    verify = Settings(_env_file=None).tls_verify  # type: ignore[call-arg]
    assert isinstance(verify, str)
    assert Path(verify) == Path("certs/ca.pem")


def test_missing_profile_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PULSE_PROFILE_NOPE_USER", raising=False)
    with pytest.raises(ProfileError, match="PULSE_PROFILE_NOPE_USER"):
        Settings(_env_file=None).profile("nope")  # type: ignore[call-arg]


def test_ca_bundle_comment_is_not_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_CA_BUNDLE", "# or leave empty to use system trust")
    assert Settings(_env_file=None).ca_bundle is None  # type: ignore[call-arg]


def test_template_matches_env_example() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (root / ".env.example").read_text(encoding="utf-8") == config.CONFIG_TEMPLATE


def test_config_discovery_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_file = tmp_path / "user" / "pulse.env"
    monkeypatch.setattr(config, "user_config_file", lambda: user_file)
    monkeypatch.delenv("PULSE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    assert config.find_config() is None  # nothing anywhere: defaults, mock only

    user_file.parent.mkdir()
    user_file.write_text("PULSE_VBR_URL=https://user.example\n", encoding="utf-8")
    assert config.find_config() == user_file

    (tmp_path / ".env").write_text("PULSE_VBR_URL=https://local.example\n", encoding="utf-8")
    assert config.find_config() == Path(".env")  # a source checkout prefers ./.env

    explicit = tmp_path / "other.env"
    monkeypatch.setenv("PULSE_CONFIG", str(explicit))
    assert config.find_config() == explicit


def test_release_build_reads_pulse_env_next_to_the_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(tmp_path / "pulse.exe"))
    monkeypatch.setattr(config, "user_config_file", lambda: tmp_path / "missing.env")
    monkeypatch.delenv("PULSE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MOCK=1\n", encoding="utf-8")
    assert config.find_config() is None  # a release never picks up a stray ./.env
    (tmp_path / "pulse.env").write_text("MOCK=1\n", encoding="utf-8")
    assert config.find_config() == tmp_path / "pulse.env"


def test_load_settings_reads_profiles_from_the_chosen_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("PULSE_VBR_URL", "PULSE_PROFILE_OPS_USER"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "lab.env"
    path.write_text(
        "PULSE_VBR_URL=https://vbr.example\nPULSE_PROFILE_OPS_USER=alice\n", encoding="utf-8"
    )
    settings = config.load_settings(path)
    assert settings.vbr_url == "https://vbr.example"
    assert settings.profile("ops").username == "alice"


def test_missing_explicit_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="pulse init"):
        config.load_settings(tmp_path / "nope.env")
