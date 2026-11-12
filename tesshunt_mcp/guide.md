# Vetting guide: how transit signals fool you

Background for reading the tools' output. It is not evidence about any star: every claim about a star must come from a tool run on that star.

## What a planet candidate needs
1. A dip that repeats at a fixed period, in every sector that observed the star with enough quality to see it.
2. A shape a small body can make: short compared with the orbit, no longer than the star's density allows, same depth on odd and even transits.
3. No second dip anywhere else in the orbit.
4. The dip located on the target star, not on a neighbour sharing its pixels.
5. No existing catalogue entry, if the claim is that it is new.

Passing all five makes a **candidate**. Confirmation takes other instruments: spectra for a mass, high-resolution imaging for hidden companions.

## Common impostors and the test that catches each
- **Eclipsing binary, circular.** A second, shallower eclipse at phase 0.5, or odd and even depths that differ (true period is twice the detected one).
- **Eclipsing binary, eccentric.** The second eclipse sits away from phase 0.5 (0.54, 0.3, anywhere). A test that looks only at 0.5 passes it. `vet_signal` scans every phase from 0.15 to 0.85; look at the full phase-curve panel yourself.
- **Spotted rotating star.** A smooth wave, not a box: the "dip" lasts a large fraction of the period and exceeds the physical maximum duration. Periods under a day with durations of many hours are almost always this.
- **Neighbour's eclipse.** TESS pixels are 21 arcseconds wide, so several stars share one. The difference image peaks off the target and the light centroid shifts during the dip. `neighbors` lists which stars are bright enough to fake the depth.
- **Diluted depth.** Extra light in the aperture makes a dip shallower, so the body looks smaller than it is. Depth that changes from sector to sector usually means the aperture changed, not the star.
- **Scattered light and pointing jitter.** Dips that line up with the start or end of an orbit, or that appear in one sector only. A real planet transits every period.
- **Single-cadence glitches.** One very low point. The pipeline drops isolated ones, but check that a "transit" is made of several consecutive points.

## Reading the numbers
- **SNR** of a search peak is its height above the scatter of the periodogram. Below about 8 expect noise. Every light curve returns a best peak.
- **depth_sigma** is depth divided by its uncertainty in the folded data. A real signal is many sigma in the fold even when the search SNR is modest.
- **Two transits in one sector** is the minimum to define a period and is weak evidence. Long periods need several sectors.
- **A transit-derived stellar density that disagrees with the catalogue** hints at an eccentric orbit or a blend.
- **An on-target verdict** does not rule out a companion inside the same pixel. TESS cannot separate stars closer than about 4 arcseconds.
- **Statistical validation tools** model the primary dip and the field. They do not see a secondary eclipse, so they cannot replace the tests above.

## What the verdicts mean
- `vet_signal`: `eclipsing_binary` (a second dip that recurs every orbit), `not_significant` (under 4 sigma against the measured scatter), `not_transit_shaped` (too long, too wide or odd/even mismatch), otherwise where the dip is: `on_target`, `likely_on_target`, `blended`, `anomaly`, `off_target`, `untestable`.
- Follow-up status: `repeats` (at least two sectors agree, none contradicts, stitched dip over 7 sigma), `does_not_repeat` (a sector that could have seen it did not), `eclipsing_binary` (second dip seen separately in at least half the sectors), `not_significant`, `inconclusive`, `single_sector_only`.
- "One deep event at phase X" means a dip seen in a single orbit: an eclipse and a glitch look alike there, and only other sectors can tell.

## Leads and the board
- A lead is a star where a bulk search of many thousands of stars saw a repeating dip. The search is deliberately generous, so most leads are impostors. Roughly 1 in 10 survives.
- Clearing an impostor is real work: it takes the star off everyone's list.
- A lead reaches the board only when two different hunters call it a candidate and the server's own results agree. The board lists unconfirmed candidates; nothing is submitted anywhere.

## Limits of this server
- Full-frame-image photometry with a simple aperture. Faint stars (TESS magnitude 15 and beyond) are noisy; saturated stars (brighter than about 6.5) bleed.
- Period search covers 0.2 to 30 days and durations of 0.5 to 6 hours.
- Neighbours come from Gaia DR3, or from the TESS Input Catalog when the Gaia archive is unreachable. The result says which.
- An archive outage is an error to retry, never a statement about the star.
