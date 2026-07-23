from exohunt.cli import _catalog_ephemerides, _known_transiting_periods


def test_incomplete_confirmed_rows_still_count_as_known_planets():
    catalog = {
        "tois": [
            {
                "toi": "1.01",
                "tfopwg_disp": "KP",
                "pl_orbper": "1.2",
                "pl_tranmid": "2459000.0",
                "pl_trandurh": "1.5",
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
    assert len(_catalog_ephemerides(catalog)) == 1
