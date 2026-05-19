from decimal import Decimal

from backtest import max_drawdown_from_curve, monte_carlo_from_equity_curve


def test_max_drawdown_from_curve() -> None:
    curve = [Decimal("100"), Decimal("105"), Decimal("98"), Decimal("110")]

    assert max_drawdown_from_curve(curve) == Decimal("7")


def test_monte_carlo_from_equity_curve_is_deterministic() -> None:
    curve = [Decimal("100"), Decimal("101"), Decimal("99"), Decimal("103"), Decimal("102")]

    first = monte_carlo_from_equity_curve(curve, simulations=25, seed=7)
    second = monte_carlo_from_equity_curve(curve, simulations=25, seed=7)

    assert first == second
    assert first["enabled"] is True
    assert first["simulations"] == 25
    assert Decimal(str(first["ending_equity_p05"])) > 0
    assert Decimal(str(first["max_drawdown_p95"])) >= 0
