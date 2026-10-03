from research.drawdown_study import qualifies


PROTOCOL = {"minimum_drawdown_reduction": .01, "minimum_return_retention": .85,
            "maximum_sharpe_loss": .1}


def test_drawdown_selection_requires_both_risk_and_return():
    volume = {"return": .20, "max_drawdown": .18, "sharpe": 1.0}
    original = {"return": .15, "max_drawdown": .17, "sharpe": .9}
    good = {"return": .18, "max_drawdown": .15, "sharpe": 1.0}
    assert qualifies(good, volume, original, PROTOCOL)
    assert not qualifies({**good, "return": .10}, volume, original, PROTOCOL)
    assert not qualifies({**good, "max_drawdown": .175}, volume, original, PROTOCOL)
    assert not qualifies({**good, "sharpe": .8}, volume, original, PROTOCOL)


def test_losing_reference_must_not_be_made_worse():
    volume = {"return": -.10, "max_drawdown": .18, "sharpe": -.3}
    original = {"return": -.12, "max_drawdown": .20, "sharpe": -.5}
    assert qualifies({"return": -.09, "max_drawdown": .15, "sharpe": -.25}, volume, original, PROTOCOL)
    assert not qualifies({"return": -.11, "max_drawdown": .15, "sharpe": -.25}, volume, original, PROTOCOL)
