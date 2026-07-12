# song_gen

Converts a MIDI file into a ukulele chord chart, and can render that
chart back out as an mp3 so you can sanity-check the detection by ear.

## Files

- `midi_to_uke.py` -- the three-stage pipeline (parse MIDI -> detect
  chords -> map to ukulele fret diagrams) and its CLI.
- `simulate_play.py` -- runs the pipeline on a MIDI file and renders a
  strummed-ukulele mp3 of the detected chord chart.
- `tests/test_midi_to_uke.py` -- end-to-end test against a synthetic
  MIDI file (C major triad for 2 beats, then G major for 2 beats).

## Install

```
pip install -r requirements.txt
```

`simulate_play.py` also needs the `ffmpeg` binary on PATH (e.g.
`apt install ffmpeg`) to encode the mp3.

## Usage

```
python midi_to_uke.py song.mid --track guitar --window quarter --format lead-sheet
python midi_to_uke.py song.mid --format json > chords.json
python simulate_play.py song.mid --track guitar -o song_uke.mp3
```

`--track` accepts a track index, a case-insensitive name substring
(e.g. `guitar`), or `all` to merge every non-drum track into one note
stream. If you don't pass `--track` and no single track name matches
"guitar", `midi_to_uke.py` lists the available tracks and prompts for
one. `simulate_play.py` defaults to `--track all` instead, since it's
meant to run non-interactively.

`--window` sets the chord-detection grid: `quarter` or `half` note, or
`measure` (aligned to the MIDI's downbeats).

## Known limitation: chord detection is unreliable on non-block-chord tracks

Stage 2 collects the set of distinct pitches sounding within each grid
window and template-matches them to a chord. That works well on a
track that's playing clean rhythm-guitar block chords, but it
struggles on tracks that are arpeggiated, single-note melody/lead
lines, or heavily ornamented -- at a small window (`quarter`), an
arpeggio's notes may not all fall in the same window and get read as
"no chord" (`N.C.`) or the wrong partial chord; a melody line rarely
has 3+ simultaneous pitches at all.

To sanity-check a track:
- Try a larger `--window` (`half` or `measure`) -- it gives more notes
  a chance to accumulate in each window at the cost of rhythmic detail.
- Look at how many spans come back as `N.C.` (`(no chord detected)` in
  `chart` format, `"chord": "N.C."` with `"voicing": null` in `json`
  output). A track with mostly `N.C.` spans probably isn't a good fit
  for this pipeline as-is.
- Listen to `simulate_play.py`'s mp3 output against the original track;
  gaps in the ukulele audio line up with `N.C.` spans.

## Ukulele voicing table

`midi_to_uke.py` doesn't hand-type its GCEA fret diagrams -- for every
root/quality combination it brute-force searches fret combinations
0-9 per string for the easiest (lowest total fret) combination whose
pitch classes exactly match the chord (see `build_ukulele_chords()` /
`_generate_voicing()`). This reproduces the standard open-position
shapes (e.g. C = `0003`, G = `0232`, Am = `2000`) and guarantees every
generated diagram is actually the requested chord. If no clean voicing
exists within that fret range the table entry is `None`, and both the
chart output and `simulate_play.py` flag it as unavailable rather than
guessing.
