import numpy as np

from exohunt.cli import _compact_sector_subset
from exohunt.pixel import difference_image, target_pixel_from_sky_grid


def test_difference_image_locates_dimming_source():
    rng = np.random.default_rng(11)
    time = np.arange(0.0, 12.0, 10.0 / (24 * 60))
    rows, columns = np.indices((5, 5))
    source_row, source_column = 1.2, 3.1
    psf = 1000.0 * np.exp(
        -0.5 * ((rows - source_row) ** 2 + (columns - source_column) ** 2) / 0.6**2
    )
    cube = 100.0 + psf[None, :, :] + rng.normal(0.0, 0.5, (time.size, 5, 5))
    period = 2.0
    epoch = 0.5
    duration_hours = 2.0
    phase = np.abs(((time - epoch + period / 2) % period) - period / 2)
    cube[phase <= (duration_hours / 24.0) / 2] -= 0.02 * psf

    result = difference_image(time, cube, period, epoch, duration_hours)

    assert result["in_transit_cadences"] > 10
    assert abs(result["centroid_row"] - source_row) < 0.3
    assert abs(result["centroid_column"] - source_column) < 0.3


def test_target_pixel_and_compact_sector_selection():
    ra = np.array([[10.02, 10.01], [10.02, 10.01]])
    dec = np.array([[20.01, 20.01], [20.00, 20.00]])
    row, column = target_pixel_from_sky_grid(ra, dec, 10.0101, 20.0001)
    assert (row, column) == (1.0, 1.0)
    assert _compact_sector_subset([1, 2, 27, 28, 29], 3) == [27, 28, 29]
