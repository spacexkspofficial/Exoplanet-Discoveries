from exohunt.tce import _period_relation


def test_period_relation_distinguishes_matches_from_unrelated_periods():
    relation, error = _period_relation(14.4715, 14.449301015)
    assert relation == "exact"
    assert error < 0.01

    relation, error = _period_relation(1.40149, 13.008806962)
    assert relation == "none"
    assert error > 0.01
