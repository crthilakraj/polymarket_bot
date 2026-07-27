import pytest

from backtest.metrics import brier_score, log_loss, max_drawdown, sharpe_ratio


# --- brier_score -----------------------------------------------------------------


def test_brier_score_is_zero_for_perfect_predictions():
    assert brier_score([1.0, 0.0, 1.0], [1.0, 0.0, 1.0]) == 0.0


def test_brier_score_is_one_for_maximally_wrong_predictions():
    assert brier_score([0.0, 1.0], [1.0, 0.0]) == 1.0


def test_brier_score_of_always_half_on_fifty_fifty_outcomes():
    assert brier_score([0.5, 0.5], [1.0, 0.0]) == pytest.approx(0.25)


def test_brier_score_empty_is_zero():
    assert brier_score([], []) == 0.0


def test_brier_score_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        brier_score([0.5], [1.0, 0.0])


# --- log_loss ----------------------------------------------------------------------


def test_log_loss_is_near_zero_for_confident_correct_predictions():
    assert log_loss([0.999], [1.0]) == pytest.approx(0.0, abs=1e-2)


def test_log_loss_penalizes_confident_wrong_predictions_heavily():
    confident_wrong = log_loss([0.99], [0.0])
    uncertain = log_loss([0.5], [0.0])
    assert confident_wrong > uncertain


def test_log_loss_clips_extreme_probabilities_to_avoid_infinity():
    result = log_loss([1.0], [0.0])
    assert result < float("inf")
    assert result > 0


def test_log_loss_empty_is_zero():
    assert log_loss([], []) == 0.0


def test_log_loss_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        log_loss([0.5], [1.0, 0.0])


# --- sharpe_ratio --------------------------------------------------------------------


def test_sharpe_ratio_is_zero_with_fewer_than_two_returns():
    assert sharpe_ratio([]) == 0.0
    assert sharpe_ratio([0.01]) == 0.0


def test_sharpe_ratio_is_zero_for_constant_returns():
    assert sharpe_ratio([0.01, 0.01, 0.01]) == 0.0


def test_sharpe_ratio_is_positive_for_consistently_positive_returns():
    assert sharpe_ratio([0.01, 0.02, 0.015, 0.012]) > 0


def test_sharpe_ratio_is_negative_for_consistently_negative_returns():
    assert sharpe_ratio([-0.01, -0.02, -0.015, -0.012]) < 0


def test_sharpe_ratio_annualizes_with_periods_per_year():
    returns = [0.01, 0.02, 0.015, 0.012]
    raw = sharpe_ratio(returns)
    annualized = sharpe_ratio(returns, periods_per_year=252)
    assert annualized == pytest.approx(raw * (252**0.5))


# --- max_drawdown --------------------------------------------------------------------


def test_max_drawdown_is_zero_for_monotonically_increasing_curve():
    assert max_drawdown([100, 110, 120, 130]) == 0.0


def test_max_drawdown_is_zero_for_short_curves():
    assert max_drawdown([]) == 0.0
    assert max_drawdown([100]) == 0.0


def test_max_drawdown_computes_largest_peak_to_trough_decline():
    # Peak at 120, trough at 90 -> (120-90)/120 = 0.25
    assert max_drawdown([100, 120, 90, 110]) == pytest.approx(0.25)


def test_max_drawdown_tracks_the_worst_of_multiple_drawdowns():
    # First drawdown: (100-90)/100 = 0.10. Second: (150-100)/150 = 0.333...
    curve = [100, 90, 100, 150, 100]
    assert max_drawdown(curve) == pytest.approx(1 / 3)
