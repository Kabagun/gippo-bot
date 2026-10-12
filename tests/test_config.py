import pytest

from gippo_bot.config import BotSettings, CabinetSettings, SettingsError


@pytest.fixture(autouse=True)
def legacy_password_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIPPO_AUTH_MODE", "password")


def test_cabinet_settings_require_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIPPO_LOGIN", raising=False)
    monkeypatch.delenv("GIPPO_PASSWORD", raising=False)

    with pytest.raises(SettingsError, match="GIPPO_LOGIN"):
        CabinetSettings.from_environment()


def test_bot_settings_parse_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_OPEN_ACCESS", "false")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123, 456")
    monkeypatch.setenv("GIPPO_LOGIN", "login")
    monkeypatch.setenv("GIPPO_PASSWORD", "password")

    settings = BotSettings.from_environment()

    assert settings.open_access is False
    assert settings.allowed_user_ids == frozenset({123, 456})


def test_bot_settings_reject_empty_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_OPEN_ACCESS", "false")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "  ")
    monkeypatch.setenv("GIPPO_LOGIN", "login")
    monkeypatch.setenv("GIPPO_PASSWORD", "password")

    with pytest.raises(SettingsError, match="TELEGRAM_OPEN_ACCESS"):
        BotSettings.from_environment()


def test_bot_settings_allow_explicit_open_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_OPEN_ACCESS", "true")
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("GIPPO_LOGIN", "login")
    monkeypatch.setenv("GIPPO_PASSWORD", "password")

    settings = BotSettings.from_environment()

    assert settings.open_access is True
    assert settings.allowed_user_ids == frozenset()


def test_bot_settings_reject_invalid_open_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_OPEN_ACCESS", "sometimes")
    monkeypatch.setenv("GIPPO_LOGIN", "login")
    monkeypatch.setenv("GIPPO_PASSWORD", "password")

    with pytest.raises(SettingsError, match="true or false"):
        BotSettings.from_environment()


def test_bot_settings_accept_card_image_path_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    gippo_card = tmp_path / "gippo.png"
    belmarket_card = tmp_path / "belmarket.png"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_OPEN_ACCESS", "true")
    monkeypatch.setenv("GIPPO_LOGIN", "login")
    monkeypatch.setenv("GIPPO_PASSWORD", "password")
    monkeypatch.setenv("GIPPO_CARD_IMAGE_PATH", str(gippo_card))
    monkeypatch.setenv("BELMARKET_CARD_IMAGE_PATH", str(belmarket_card))

    settings = BotSettings.from_environment()

    assert settings.gippo_card_image_path == gippo_card
    assert settings.belmarket_card_image_path == belmarket_card


def sms_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("GIPPO_AUTH_MODE", "sms")
    monkeypatch.setenv("GIPPO_PHONE", "+375 (29) 123 45 67")
    monkeypatch.setenv("GIPPO_SESSION_PATH", str(tmp_path / "session.json"))
    monkeypatch.delenv("GIPPO_PASSWORD", raising=False)
    monkeypatch.delenv("SMS_FORWARDER_TOKEN", raising=False)
    monkeypatch.delenv("SMS_FORWARDER_TOKEN_FILE", raising=False)


def test_sms_mode_is_default_and_does_not_require_password(monkeypatch, tmp_path) -> None:
    sms_environment(monkeypatch, tmp_path)
    monkeypatch.delenv("GIPPO_AUTH_MODE")
    settings = CabinetSettings.from_environment()
    assert settings.auth_mode == "sms"
    assert settings.login == "+375291234567"
    assert settings.password == ""
    assert settings.session_path == tmp_path / "session.json"


def test_sms_mode_requires_persistent_session(monkeypatch, tmp_path) -> None:
    sms_environment(monkeypatch, tmp_path)
    monkeypatch.delenv("GIPPO_SESSION_PATH")
    with pytest.raises(SettingsError, match="GIPPO_SESSION_PATH"):
        CabinetSettings.from_environment()


@pytest.mark.parametrize("phone", ["login", "+123456789012", "+37529123456"])
def test_sms_mode_validates_phone(monkeypatch, tmp_path, phone) -> None:
    sms_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("GIPPO_PHONE", phone)
    with pytest.raises(SettingsError, match="GIPPO_PHONE"):
        CabinetSettings.from_environment()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("GIPPO_TIMEOUT_SECONDS", "nan"),
        ("GIPPO_SMS_TTL_SECONDS", "301"),
        ("GIPPO_SMS_COOLDOWN_SECONDS", "899"),
        ("GIPPO_AUTH_MODE", "other"),
    ],
)
def test_auth_configuration_rejects_unsafe_values(monkeypatch, tmp_path, variable, value) -> None:
    sms_environment(monkeypatch, tmp_path)
    monkeypatch.setenv(variable, value)
    with pytest.raises(SettingsError):
        CabinetSettings.from_environment()


def test_protected_sms_token_file_and_sender_override(monkeypatch, tmp_path) -> None:
    sms_environment(monkeypatch, tmp_path)
    token_file = tmp_path / "sms.token"
    token_file.write_text("consumer-secret\n")
    token_file.chmod(0o600)
    monkeypatch.setenv("SMS_FORWARDER_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("GIPPO_SMS_SENDER", "GippoSender")
    settings = CabinetSettings.from_environment()
    assert settings.sms_forwarder_token == "consumer-secret"
    assert settings.sms_sender == "GippoSender"
    assert "consumer-secret" not in repr(settings)
    monkeypatch.setenv("SMS_FORWARDER_TOKEN", "inline-secret")
    with pytest.raises(SettingsError, match="only one"):
        CabinetSettings.from_environment()


def test_automatic_sms_bot_does_not_require_owner(monkeypatch, tmp_path) -> None:
    sms_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_OPEN_ACCESS", "true")
    monkeypatch.setenv("SMS_FORWARDER_TOKEN", "consumer-token")
    monkeypatch.delenv("TELEGRAM_OWNER_USER_ID", raising=False)
    assert BotSettings.from_environment().owner_user_id is None


def test_manual_sms_bot_requires_explicit_owner(monkeypatch, tmp_path) -> None:
    sms_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_OPEN_ACCESS", "true")
    monkeypatch.delenv("TELEGRAM_OWNER_USER_ID", raising=False)
    with pytest.raises(SettingsError, match="TELEGRAM_OWNER_USER_ID"):
        BotSettings.from_environment()
    monkeypatch.setenv("TELEGRAM_OWNER_USER_ID", "123")
    assert BotSettings.from_environment().owner_user_id == 123
