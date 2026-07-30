import dataclasses
import json

from config import Settings, _load_tracked_market_ids, settings


def test_settings_defaults_apply_when_env_unset(monkeypatch):
    for var in (
        "POLYMARKET_PRIVATE_KEY",
        "CLOB_API_KEY",
        "CLOB_API_SECRET",
        "CLOB_API_PASSPHRASE",
        "MAX_POSITION_USD",
        "DRY_RUN",
    ):
        monkeypatch.delenv(var, raising=False)

    result = Settings.from_env()

    assert result.max_position_usd == 100.0
    assert result.dry_run is True
    assert result.live_max_fund_usd == 300.0


def test_require_trading_credentials_raises_when_missing():
    incomplete = dataclasses.replace(
        settings,
        private_key=None,
        clob_api_key=None,
        clob_api_secret=None,
        clob_api_passphrase=None,
    )

    try:
        incomplete.require_trading_credentials()
    except RuntimeError as exc:
        assert "POLYMARKET_PRIVATE_KEY" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for missing credentials")


def test_require_live_trading_confirmation_is_a_noop_in_dry_run():
    dry_run_settings = dataclasses.replace(settings, dry_run=True, live_trading_confirmed=False)
    dry_run_settings.require_live_trading_confirmation()  # should not raise


def test_require_live_trading_confirmation_raises_without_explicit_opt_in():
    live_but_unconfirmed = dataclasses.replace(settings, dry_run=False, live_trading_confirmed=False)

    try:
        live_but_unconfirmed.require_live_trading_confirmation()
    except RuntimeError as exc:
        assert "LIVE_TRADING_CONFIRMED" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when live trading isn't explicitly confirmed")


def test_require_live_trading_confirmation_still_checks_credentials():
    live_and_confirmed_but_no_creds = dataclasses.replace(
        settings,
        dry_run=False,
        live_trading_confirmed=True,
        private_key=None,
        clob_api_key=None,
        clob_api_secret=None,
        clob_api_passphrase=None,
    )

    try:
        live_and_confirmed_but_no_creds.require_live_trading_confirmation()
    except RuntimeError as exc:
        assert "POLYMARKET_PRIVATE_KEY" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for missing credentials")


def test_load_tracked_market_ids_falls_back_when_file_missing(tmp_path):
    missing = tmp_path / "does_not_exist.json"

    assert _load_tracked_market_ids(str(missing), fallback=["0xfallback"]) == ["0xfallback"]


def test_load_tracked_market_ids_reads_condition_ids_from_file(tmp_path):
    path = tmp_path / "tracked_markets.json"
    path.write_text(
        json.dumps(
            [
                {"condition_id": "0xa", "question": "A?"},
                {"condition_id": "0xb", "question": "B?"},
            ]
        )
    )

    assert _load_tracked_market_ids(str(path), fallback=["0xfallback"]) == ["0xa", "0xb"]


def test_load_tracked_market_ids_falls_back_on_malformed_json(tmp_path):
    path = tmp_path / "tracked_markets.json"
    path.write_text("not json")

    assert _load_tracked_market_ids(str(path), fallback=["0xfallback"]) == ["0xfallback"]


def test_load_tracked_market_ids_falls_back_on_empty_or_invalid_entries(tmp_path):
    path = tmp_path / "tracked_markets.json"
    path.write_text(json.dumps([{"question": "no condition_id here"}]))

    assert _load_tracked_market_ids(str(path), fallback=["0xfallback"]) == ["0xfallback"]


def test_load_tracked_market_ids_skips_invalid_entries_but_keeps_valid_ones(tmp_path):
    path = tmp_path / "tracked_markets.json"
    path.write_text(
        json.dumps([{"condition_id": "0xa", "question": "A?"}, {"question": "missing id"}, "not a dict"])
    )

    assert _load_tracked_market_ids(str(path), fallback=["0xfallback"]) == ["0xa"]
