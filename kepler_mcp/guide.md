# Vetting reference: false positives in transit searches

Background for reading tool output. Not evidence about any star: claims about a star come from tool runs on that star.

## Terminology
Wording follows AAS-journal and TESS follow-up usage.

| This server | In a manuscript or on ExoFOP |
|---|---|
| lead (unvetted) | transit-like signal |
| verdict `candidate` | planet candidate (PC) |
| verdict `eclipsing_binary` | false positive (FP): eclipsing binary (EB) |
| verdict `blend_or_off_target` | false positive (FP): nearby eclipsing binary (NEB), or off-target / blended signal |
| verdict `systematics_or_noise` | false alarm (FA) |
| verdict `known_object` | known planet (KP), confirmed planet (CP), TOI or CTOI: say which |
| verdict `inconclusive` | inconclusive |
| follow-up `repeats` | recovered in N of M Sectors |
| `on_target` | centroid and difference image consistent with the target |
| `off_target` | centroid offset |
| `not_transit_shaped` | not transit-like (V-shaped, too long, or odd-even mismatch) |
| patch | field, or target set |

- Disposition codes here are this server's labels in the community's style. Only the TESS Follow-up Observing Program Working Group assigns TFOPWG dispositions, and only the TESS project assigns TOI numbers. "Ambiguous planet candidate" (APC) is theirs to assign.
- Verbs: detected, identified, recovered, consistent with. "Validated" applies only with a statistical false-positive probability below 0.015 and a nearby false-positive probability below 0.001 (Giacalone et al. 2021), which this server does not compute. Never "confirmed" or "discovered" for a signal from this server.
- A secondary-eclipse result is stated with its phase range: "no secondary eclipse over phases 0.15-0.85 (< N ppm)".
- A false-positive probability is quoted only beside the all-phase secondary result.
- Times in a manuscript or an ExoFOP upload are BJD_TDB. BTJD (BJD_TDB - 2457000) is internal; every epoch also comes as `*_bjd_tdb`.
- Units: R_Earth, d, h, ppm, K. Uncertainties as value +/- 1 sigma. Significance as S/N or N sigma.
- TESS, TIC, TOI, CTOI in capitals; "Sector 7". Stellar parameters are from TIC v8 unless a result says otherwise.

## The record
A follow-up that recovers a signal returns `record`: `tic_id, tmag, epoch_bjd_tdb, period_d, depth_ppm, duration_h, rp_rearth, snr, sectors`, each with an uncertainty, plus notes (odd-even depth difference, secondary limit with phase range, Sectors recovered, stellar-parameter source). The uncertainties are box-fit estimates. A transit-model fit is required before the row goes into a table or an upload.

## What a planet candidate needs
1. A dip that repeats at a fixed period, in every sector that observed the star with enough quality to see it.
2. A shape a small body can make: short compared with the orbit, no longer than the star's density allows, same depth on odd and even transits.
3. No second dip anywhere else in the orbit.
4. The dip located on the target star, not on a neighbour sharing its pixels.
5. No existing catalogue entry, if the claim is that it is new.

Passing all five makes a **candidate**. Confirmation takes other instruments: spectra for a mass, high-resolution imaging for hidden companions.

## Common false positives and the test for each
- **Eclipsing binary, circular.** A second, shallower eclipse at phase 0.5, or odd and even depths that differ (true period is twice the detected one).
- **Eclipsing binary, eccentric.** The second eclipse sits away from phase 0.5 (0.54, 0.3, anywhere). A test that looks only at 0.5 passes it. `vet_signal` scans every phase from 0.15 to 0.85; the full phase-curve panel shows it directly.
- **Spotted rotating star.** A smooth wave, not a box: the "dip" lasts a large fraction of the period and exceeds the physical maximum duration. Periods under a day with durations of many hours are almost always this.
- **Neighbour's eclipse.** TESS pixels are 21 arcseconds wide, so several stars share one. The difference image peaks off the target and the light centroid shifts during the dip. `neighbors` lists which stars are bright enough to fake the depth.
- **Diluted depth.** Extra light in the aperture makes a dip shallower, so the body looks smaller than it is. Depth that changes from sector to sector usually means the aperture changed, not the star.
- **Scattered light and pointing jitter.** Dips that line up with the start or end of an orbit, or that appear in one sector only. A real planet transits every period.
- **Single-cadence glitches.** One very low point. The pipeline drops isolated ones; a real transit spans several consecutive points.

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
- New leads come from **patches**: about 150 unsearched stars of one type, searched on the user's own computer in about seven minutes. The hub re-runs every reported signal itself before it becomes a lead. Many patches find nothing; that sky is then cleared for everyone.
- A lead is a star where a bulk search of many thousands of stars saw a repeating dip. The search is deliberately generous, so most leads are impostors. Roughly 1 in 10 survives.
- A lead reaches the board only when two different hunters call it a candidate and the server's own results agree. The board lists unconfirmed candidates; nothing is submitted anywhere.

## Limits of this server
- Full-frame-image photometry with a simple aperture. Faint stars (TESS magnitude 15 and beyond) are noisy; saturated stars (brighter than about 6.5) bleed.
- Period search covers 0.2 to 30 days and durations of 0.5 to 6 hours.
- Neighbours come from Gaia DR3, or from the TESS Input Catalog when the Gaia archive is unreachable. The result says which.
- An archive outage is an error to retry, never a statement about the star.
