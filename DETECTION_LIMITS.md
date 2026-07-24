# What this TESS search can and cannot see

## The short version

This project is best at finding **large, short-period planets that cross the
face of relatively bright, quiet stars**. It is deliberately a screening
pipeline, not a machine that can decide whether every star has a planet.

A null result means:

> No repeating, transit-shaped signal strong enough for this pipeline was found
> in the downloaded TESS window.

It does **not** mean that the star has no planets.

## The four main visibility gates

### 1. The orbit must line up

TESS measures transits: a planet must pass between us and its star. Most orbital
planes do not line up that way. The probability is approximately
`(star radius + planet radius) / orbital distance`, so close-in planets are much
more likely to transit than distant ones.

For a Sun-like star, an Earth-distance orbit must be aligned to within roughly
0.27 degrees of edge-on. A hot planet at 0.05 AU has an alignment window of
roughly 5 degrees. A perfectly real planet outside that narrow viewing geometry
is invisible to this workflow.

### 2. The dip must be deep enough

Transit depth is approximately:

`(planet radius / star radius)²`

An Earth crossing the Sun dims it by only about 84 parts per million. Jupiter
crossing the Sun produces a dip near 1 percent. The same planet is easier to see
around a smaller star and harder around a larger star.

Faint targets, active stars, instrumental noise, and light from neighboring
stars can bury or dilute a real signal. The current campaign searches official
TESS targets with TESS magnitude 7–14; it has no explicit distance cutoff.
Distance matters indirectly because more distant stars are usually fainter, not
because the transit method has a fixed parsec boundary.

### 3. Enough transits must occur during observation

One TESS sector is observed for roughly 27 days. The present campaign searches
periods from **0.5 through 13 days**, which is intentionally short enough to
offer about two or more events in a normal one-sector window. A planet taking
50 days or one year to orbit will usually show zero or one transit in that
window. A single event is retained as a longer-baseline lead, but it cannot
supply a secure repeating period by itself.

Targets observed in multiple TESS sectors—especially near the continuous
viewing zones—can support much longer-period searches after their sectors are
stitched together. This campaign does not yet perform that multi-sector search
for every target.

### 4. The signal must survive the screen

The software detrends each light curve, searches box-shaped repeating dips, and
tests the strongest signal for obvious problems. Real transits can still be
missed because of:

- data gaps or a transit occurring outside the observing window;
- stellar rotation, spots, flares, or correlated noise;
- TESS's roughly 21-arcsecond pixels blending neighboring stars;
- very short events being smeared by cadence;
- eccentric or grazing transits that do not resemble the simple search model;
- a stronger variable or false-positive signal hiding a weaker planet; or
- detrending that partly removes a long or shallow event.

Conversely, an automated survivor can still be an eclipsing binary, background
blend, stellar variability, or spacecraft systematic. It is a follow-up lead,
not a planet claim.

## What the current 10,000-star campaign samples

The campaign is two sequential, non-overlapping groups:

1. 5,000 Sector 105 targets already in progress.
2. 5,000 new Sector 100 targets queued behind them.

Both groups use official TESS target lists, span all 16 camera/CCD combinations,
use TESS magnitude 7–14, and search periods of 0.5–13 days. Choosing a different
sector increases sky and stellar diversity without changing the scientific
meaning of the first run. Older results that lack newer diagnostics remain
marked as legacy-unmeasured; a targeted legacy recheck can be run separately
without replacing 5,000 new observations.

## What each result label really means

- **No transit detected:** no qualifying repeating dip in this data window.
  The star may still host smaller, longer-period, non-transiting, or obscured
  planets.
- **Strongest signal screened out:** that particular signal looked implausible.
  We did not prove the star planet-free or exhaustively rule out weaker signals.
- **Single-event lead:** something transit-like happened once; more time
  coverage is needed.
- **Automated survivor:** a signal passed the first screen and needs pixel,
  catalog, independent-reduction, and multi-sector vetting.

## How to see the planets TESS misses

No single method sees every planet:

- **More TESS sectors** extend the time baseline.
- **Independent TESS reductions** such as SPOC, QLP, and TGLC test whether a
  feature survives another extraction.
- **Radial velocity** finds gravitational motion, including many non-transiting
  planets, and can measure mass.
- **Gaia astrometry** is most useful for larger, wider-orbit companions around
  suitable nearby stars.
- **Microlensing** can find colder and more distant planets but usually does not
  provide repeat observations.
- **Direct imaging** targets young, hot, widely separated planets around
  favorable stars.

The next scientific improvement is therefore not to call more null results
planet-free. It is to measure injection/recovery completeness, combine repeated
TESS sectors, search weaker residual signals, and route the best survivors into
independent data and follow-up methods.

## Primary references

- [TESS Instrument Handbook (MAST)](https://archive.stsci.edu/files/live/sites/mast/files/home/missions-and-data/active-missions/tess/_documents/TESS_Instrument_Handbook_v0.1.pdf)
- [TESS mission science and observing strategy (MIT)](https://tess.mit.edu/science/)
- [Ricker et al. 2015, the TESS mission paper](https://doi.org/10.1007/s11214-014-0131-9)
- [Winn 2010, Transits and Occultations](https://arxiv.org/abs/1001.2010)
