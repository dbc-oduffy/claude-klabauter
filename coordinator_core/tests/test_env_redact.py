"""redact_env masks secret-named values and keeps the rest."""

from __future__ import annotations

from coordinator_core.env_redact import REDACTED, is_secret_name, redact_env
from coordinator_core.warm.door_credential import CREDENTIAL_ENV


def test_secret_names_masked_and_path_kept():
    out = redact_env(
        {"COORDINATOR_DOOR_KEY": "x", "GH_TOKEN": "y", "MY_SECRET_X": "z", "PATH": "/bin"}
    )
    assert out == {
        "COORDINATOR_DOOR_KEY": REDACTED,
        "GH_TOKEN": REDACTED,
        "MY_SECRET_X": REDACTED,
        "PATH": "/bin",
    }


def test_door_credential_env_is_masked_by_name():
    assert is_secret_name(CREDENTIAL_ENV)
    assert redact_env({CREDENTIAL_ENV: "leak"})[CREDENTIAL_ENV] == REDACTED


def test_matching_is_case_insensitive():
    assert is_secret_name("api_key")
    assert is_secret_name("Gh_Token")
    assert is_secret_name("clientsecret")
    assert is_secret_name("DB_PASSWORD")
    assert is_secret_name("AWS_CREDENTIALS_FILE")


def test_non_secret_names_kept():
    for name in ("KEYBOARD", "TOKENIZER_MODE", "COORDINATOR_WARM_RUNTIME_BASE", "PYTEST_CURRENT_TEST"):
        assert not is_secret_name(name)
    assert redact_env({"KEYBOARD": "us"}) == {"KEYBOARD": "us"}


def test_input_not_mutated():
    src = {"A_KEY": "v"}
    redact_env(src)
    assert src == {"A_KEY": "v"}
