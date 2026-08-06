from pathlib import Path

from scripts import prefetch_light_curves as prefetch


def test_prefetch_budget_does_not_rescan_whole_cache_per_report(
    tmp_path: Path, monkeypatch
) -> None:
    cache_root = tmp_path / "cache"
    cache_scans: list[Path] = []
    tick = 0.0

    def monotonic() -> float:
        nonlocal tick
        tick += 11.0
        return tick

    def cache_bytes(path: Path) -> int:
        cache_scans.append(path)
        return 0

    monkeypatch.setenv("EXOHUNT_CACHE_DIR", str(cache_root))
    monkeypatch.setattr(prefetch, "resolve_cache_dir", lambda *_a, **_kw: cache_root)
    monkeypatch.setattr(prefetch, "cache_bytes", cache_bytes)
    monkeypatch.setattr(prefetch.clock, "monotonic", monotonic)
    monkeypatch.setattr(prefetch, "_read_target_rows", lambda _path: [{}])
    monkeypatch.setattr(
        prefetch,
        "_batch_target_spec",
        lambda *_a, **_kw: {
            "target": "TIC 1",
            "tic_id": 1,
            "sectors": [97],
        },
    )
    monkeypatch.setattr(prefetch, "already_cached", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        prefetch,
        "sector_product_index",
        lambda *_a, **_kw: {1: ("observation", "https://example.invalid/file")},
    )
    monkeypatch.setattr(prefetch, "direct_download", lambda *_a, **_kw: 100)

    assert (
        prefetch.main(
            [
                "--targets",
                str(tmp_path / "targets.csv"),
                "--direct",
                "--workers",
                "1",
                "--max-gb",
                "1",
            ]
        )
        == 0
    )

    # One exact scan establishes the starting size and one records the final
    # report. Progress updates use the byte counter instead of walking the root.
    assert cache_scans == [cache_root, cache_root]
