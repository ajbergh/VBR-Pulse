"""Settings (PLAN §8) and account-profile secret resolution.

Secrets resolution order: OS keyring → environment variable → vault reference.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import keyring
from dotenv import dotenv_values
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pulse.vbr.client import Credentials
from pulse.vbr.operations import SPEC_VERSION

KEYRING_SERVICE = "vbr-pulse"
VAULT_SCHEME = "vault://"
ENV_FILE = Path(".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    vbr_url: str = Field(default="https://localhost", alias="PULSE_VBR_URL")
    api_version: str = Field(default=SPEC_VERSION, alias="PULSE_API_VERSION")
    ca_bundle: Path | None = Field(default=None, alias="PULSE_CA_BUNDLE")
    lab_insecure_tls: bool = Field(default=False, alias="PULSE_LAB_INSECURE_TLS")
    profiles_csv: str = Field(default="ops,ir,view", alias="PULSE_PROFILES")
    poll_seconds: int = Field(default=5, alias="PULSE_POLL_SECONDS")
    mock: bool = Field(default=False, alias="MOCK")
    mock_scenario: str = Field(default="happy", alias="MOCK_SCENARIO")

    @field_validator("ca_bundle", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        return None if value in ("", None) else value

    @property
    def mode(self) -> Literal["live", "mock"]:
        return "mock" if self.mock else "live"

    @property
    def tls_verify(self) -> bool | str:
        if self.lab_insecure_tls:
            return False
        return str(self.ca_bundle) if self.ca_bundle else True

    @property
    def profile_names(self) -> list[str]:
        return [p.strip().lower() for p in self.profiles_csv.split(",") if p.strip()]

    def profile(self, name: str) -> Profile:
        env = _environment(ENV_FILE)
        key = f"PULSE_PROFILE_{name.upper()}"
        username = env.get(f"{key}_USER")
        if not username:
            raise ProfileError(f"Profile '{name}' has no {key}_USER setting.")
        return Profile(name=name.lower(), username=username, secret_ref=env.get(f"{key}_SECRET"))

    def profiles(self) -> list[Profile]:
        return [self.profile(name) for name in self.profile_names]


class ProfileError(Exception):
    """A profile is missing or its secret can't be resolved. Message is safe to display."""


@dataclass(frozen=True)
class Profile:
    """An account shown on the sign-in screen. Holds a secret *reference*, never the secret."""

    name: str
    username: str
    secret_ref: str | None

    def credentials(self) -> Credentials:
        return Credentials(username=self.username, password=resolve_secret(self))


def _environment(env_file: Path) -> dict[str, str]:
    file_values = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    return {**file_values, **os.environ}


def _keyring_get(service: str, username: str) -> str | None:
    try:
        return keyring.get_password(service, username)
    except Exception:
        return None


def resolve_secret(profile: Profile) -> SecretStr:
    # 1. OS keyring, keyed by the account name.
    if secret := _keyring_get(KEYRING_SERVICE, profile.username):
        return SecretStr(secret)
    ref = profile.secret_ref or ""
    # 2. A literal value in the environment / .env.
    if ref and not ref.startswith(VAULT_SCHEME):
        return SecretStr(ref)
    # 3. vault://<service>/<name> — resolved through the OS keyring's secret store.
    if ref.startswith(VAULT_SCHEME):
        service, _, name = ref.removeprefix(VAULT_SCHEME).partition("/")
        if service and name and (secret := _keyring_get(service, name)):
            return SecretStr(secret)
    raise ProfileError(
        f"No secret found for profile '{profile.name}' ({profile.username}). "
        f"Store it with: keyring set {KEYRING_SERVICE} {profile.username}"
    )
