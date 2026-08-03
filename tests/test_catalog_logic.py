from exohunt.cli import _catalog_ephemerides, _known_transiting_periods


def test_incomplete_confirmed_rows_still_count_as_known_planets():
    catalog = {
        "tois": [
            {
                "toi": "1.01",
                "tfopwg_disp": "KP",
                "pl_orbper": "1.2",
                "pl_orbpererr1": "0.002",
                "pl_orbpererr2": "-0.003",
                "pl_tranmid": "2459000.0",
                "pl_tranmiderr1": "0.004",
                "pl_tranmiderr2": "-0.005",
                "pl_trandurh": "1.5",
                "pl_trandurherr1": "0.1",
                "pl_trandurherr2": "-0.2",
            }
        ],
        "confirmed_planets": [
            {
                "pl_name": "Test b",
                "pl_orbper": "1.2",
                "pl_tranmid": "2459000.0",
                "pl_trandur": "",
                "tran_flag": "1",
            },
            {
                "pl_name": "Test c",
                "pl_orbper": "3.6",
                "pl_tranmid": "2459001.0",
                "pl_trandur": "",
                "tran_flag": "1",
            },
        ],
    }
    assert _known_transiting_periods(catalog) == [1.2, 3.6]
    events = _catalog_ephemerides(catalog)
    assert len(events) == 1
    assert events[0]["period_uncertainty_days"] == 0.003
    assert events[0]["epoch_uncertainty_days"] == 0.005
    assert events[0]["duration_uncertainty_hours"] == 0.2


def test_incomplete_ephemeris_period_remains_visible_to_recovery_screening():
    catalog = {
        "tois": [],
        "confirmed_planets": [
            {
                "pl_name": "Incomplete b",
                "pl_orbper": "2.75485",
                "pl_tranmid": "",
                "pl_trandur": "",
                "tran_flag": "1",
            }
        ],
    }

    assert _catalog_ephemerides(catalog) == []
    assert _known_transiting_periods(catalog) == [2.75485]
