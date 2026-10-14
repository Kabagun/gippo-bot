import json
from unittest.mock import Mock

import pytest
from test_cache import instant, provider, status

from gippo_bot import cli
from gippo_bot.config import CabinetSettings


def settings(tmp_path) -> CabinetSettings:
    return CabinetSettings(
        login="login", password="secret", auth_mode="password", cache_path=tmp_path / "cache.json"
    )


@pytest.mark.parametrize("empty", [True, False])
def test_check_reads_cache_only_and_never_builds_provider(
    monkeypatch, tmp_path, capsys, empty
) -> None:
    configured = settings(tmp_path)
    if not empty:
        configured.status_cache().run_scheduled(lambda: provider(status()), now=instant)
    monkeypatch.setattr(CabinetSettings, "from_environment", classmethod(lambda cls: configured))
    factory = Mock(side_effect=AssertionError("A cache read must never construct a provider"))
    monkeypatch.setattr(CabinetSettings, "create_client", factory)
    monkeypatch.setattr("sys.argv", ["gippo-check", "--json"])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    if empty:
        assert result["status"] is None and "next_update" in result
    else:
        assert result["monthly_purchases"] == "247.27"
    factory.assert_not_called()


def test_check_invalid_cache_does_not_fetch_live(monkeypatch, tmp_path) -> None:
    configured = settings(tmp_path)
    configured.cache_path.write_text("bad")
    configured.cache_path.chmod(0o600)
    monkeypatch.setattr(CabinetSettings, "from_environment", classmethod(lambda cls: configured))
    factory = Mock()
    monkeypatch.setattr(CabinetSettings, "create_client", factory)
    monkeypatch.setattr("sys.argv", ["gippo-check"])
    with pytest.raises(SystemExit, match="protected"):
        cli.main()
    factory.assert_not_called()


def test_manual_check_outside_active_cycle_never_prompts_or_calls_provider(
    monkeypatch, tmp_path
) -> None:
    configured = settings(tmp_path)
    monkeypatch.setattr(CabinetSettings, "from_environment", classmethod(lambda cls: configured))
    factory, prompt = (
        Mock(),
        Mock(side_effect=AssertionError("Must not prompt outside an active cycle")),
    )
    monkeypatch.setattr(CabinetSettings, "create_client", factory)
    monkeypatch.setattr("getpass.getpass", prompt)
    monkeypatch.setattr("sys.argv", ["gippo-check", "--sms-code"])
    with pytest.raises(SystemExit, match="no active scheduled"):
        cli.main()
    factory.assert_not_called()
    prompt.assert_not_called()
