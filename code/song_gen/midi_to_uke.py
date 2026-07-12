"""
midi_to_uke.py
==============

Converts a MIDI file into a ukulele chord chart with timings.

Stage 1 - Parse MIDI
    Load the file with pretty_midi and pick a track (by index, name
    substring such as "guitar", or "all" to merge every track).

Stage 2 - Chord detection
    Segment the timeline into a grid aligned to the MIDI's beat/downbeat
    information, collect the active pitch classes in each window, and
    match them against a chord template dictionary by best-fit (Jaccard)
    scoring. Windows with fewer than 3 distinct pitches are flagged as
    "no chord" rather than guessed. Consecutive windows with the same
    chord are merged into a single span.

Stage 3 - Map to ukulele
    Each chord name is looked up in a GCEA fret-diagram table. The table
    is generated (not hand-typed) by brute-force searching, for every
    root/quality combination, the lowest-effort fret-per-string
    combination whose pitch classes exactly match the chord's pitch
    classes -- see build_ukulele_chords().

See README.md for the documented limitations of window-based chord
detection on arpeggiated or single-note tracks.
"""

import argparse
import itertools
import json
import sys
from collections import namedtuple

import pretty_midi

# ---------------------------------------------------------------------------
# Music theory constants
# ---------------------------------------------------------------------------
PITCH_CLASS_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# chord quality suffix -> semitone intervals above the root
CHORD_TEMPLATES = {
    "":     (0, 4, 7),       # major
    "m":    (0, 3, 7),       # minor
    "7":    (0, 4, 7, 10),   # dominant 7th
    "maj7": (0, 4, 7, 11),
    "m7":   (0, 3, 7, 10),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
    "dim":  (0, 3, 6),
    "aug":  (0, 4, 8),
}

MIN_CHORD_NOTES = 3          # fewer distinct pitch classes than this => "no chord"
CHORD_MATCH_THRESHOLD = 0.5  # minimum Jaccard score to accept the best-fit template
NO_CHORD = "N.C."

# GCEA reentrant tuning (high G, C, E, A), as pitch classes and as the
# reference-octave MIDI note numbers used for audio synthesis.
UKULELE_OPEN_PITCH_CLASSES = (7, 0, 4, 9)     # G, C, E, A
UKULELE_OPEN_MIDI_PITCHES = (67, 60, 64, 69)  # G4, C4, E4, A4
UKULELE_MAX_FRET = 9

NoteEvent = namedtuple("NoteEvent", ["pitch", "start", "end", "velocity"])


# ---------------------------------------------------------------------------
# Stage 1: parse MIDI
# ---------------------------------------------------------------------------
def load_midi(path):
    return pretty_midi.PrettyMIDI(path)


def describe_tracks(pm):
    return [
        "{}: {} (program={}, notes={})".format(i, inst.name or "(unnamed)", inst.program, len(inst.notes))
        for i, inst in enumerate(pm.instruments)
        if not inst.is_drum
    ]


def select_notes(pm, track=None, prompt=input):
    """Return the pretty_midi Notes for the requested track(s).

    track may be None (try to auto-match a "guitar" track, else ask),
    an int/str index, a case-insensitive name substring, or "all" to
    merge every non-drum instrument.
    """
    candidates = [(i, inst) for i, inst in enumerate(pm.instruments) if not inst.is_drum]
    if not candidates:
        raise ValueError("MIDI file has no non-drum instrument tracks.")

    if track is not None:
        if str(track).strip().lower() == "all":
            chosen = candidates
        else:
            try:
                idx = int(track)
                chosen = [c for c in candidates if c[0] == idx]
                if not chosen:
                    raise ValueError("No track at index {}.".format(idx))
            except ValueError:
                needle = str(track).strip().lower()
                chosen = [c for c in candidates if needle in (c[1].name or "").lower()]
                if not chosen:
                    raise ValueError("No track name matches '{}'.".format(track))
        notes = []
        for _, inst in chosen:
            notes.extend(inst.notes)
        return sorted(notes, key=lambda n: n.start)

    guitar_matches = [c for c in candidates if "guitar" in (c[1].name or "").lower()]
    if len(guitar_matches) == 1:
        return sorted(guitar_matches[0][1].notes, key=lambda n: n.start)

    print("No single guitar track found. Available tracks:", file=sys.stderr)
    for line in describe_tracks(pm):
        print("  " + line, file=sys.stderr)
    choice = prompt("Enter a track index, name, or 'all' to merge every track: ").strip()
    return select_notes(pm, choice, prompt=prompt)


def extract_note_events(notes):
    return [NoteEvent(n.pitch, n.start, n.end, n.velocity) for n in notes]


# ---------------------------------------------------------------------------
# Stage 2: chord detection
# ---------------------------------------------------------------------------
def get_window_boundaries(pm, window="quarter"):
    end_time = pm.get_end_time()
    if window == "measure":
        points = list(pm.get_downbeats())
    elif window == "half":
        points = list(pm.get_beats())[::2]
    elif window == "quarter":
        points = list(pm.get_beats())
    else:
        raise ValueError("Unknown window size '{}' (expected quarter, half, or measure).".format(window))

    points = sorted(set(p for p in points if p <= end_time) | {0.0, end_time})
    return points


def active_pitch_classes(notes, start, end):
    return {n.pitch % 12 for n in notes if n.start < end and n.end > start}


def match_chord(pitch_classes):
    """Best-fit template match by Jaccard score; None means "no chord"."""
    if len(pitch_classes) < MIN_CHORD_NOTES:
        return None

    best_score, best_name = -1.0, None
    for root in range(12):
        for suffix, intervals in CHORD_TEMPLATES.items():
            template = frozenset((root + iv) % 12 for iv in intervals)
            union = pitch_classes | template
            score = len(pitch_classes & template) / len(union) if union else 0.0
            if score > best_score:
                best_score, best_name = score, PITCH_CLASS_NAMES[root] + suffix

    return best_name if best_score >= CHORD_MATCH_THRESHOLD else None


def detect_chords(notes, boundaries):
    """[(chord_name_or_N.C., start, end), ...], consecutive equal spans merged."""
    spans = []
    for start, end in zip(boundaries, boundaries[1:]):
        if end - start < 1e-6:
            continue
        chord = match_chord(active_pitch_classes(notes, start, end))
        spans.append([chord if chord is not None else NO_CHORD, start, end])

    merged = []
    for label, start, end in spans:
        if merged and merged[-1][0] == label:
            merged[-1][2] = end
        else:
            merged.append([label, start, end])
    return [tuple(span) for span in merged]


# ---------------------------------------------------------------------------
# Stage 3: map to ukulele
# ---------------------------------------------------------------------------
def _generate_voicing(root, intervals, max_fret=UKULELE_MAX_FRET):
    """Lowest-effort fret-per-string combo whose pitch classes exactly
    match the chord (every chord tone present, no extra tones)."""
    required = frozenset((root + iv) % 12 for iv in intervals)
    best = None
    for frets in itertools.product(range(max_fret + 1), repeat=4):
        covered = frozenset((UKULELE_OPEN_PITCH_CLASSES[s] + frets[s]) % 12 for s in range(4))
        if covered != required:
            continue
        key = (sum(frets), max(frets), frets)
        if best is None or key < best[0]:
            best = (key, frets)
    return best[1] if best else None


def build_ukulele_chords():
    """chord name (e.g. "C", "G7", "Bbm7") -> 4-char fret diagram string,
    or None if no clean voicing exists within UKULELE_MAX_FRET."""
    table = {}
    for root in range(12):
        for suffix, intervals in CHORD_TEMPLATES.items():
            frets = _generate_voicing(root, intervals)
            name = PITCH_CLASS_NAMES[root] + suffix
            table[name] = "".join(str(f) for f in frets) if frets else None
    return table


UKULELE_CHORDS = build_ukulele_chords()


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------
def format_chart(chords):
    lines = []
    for name, start, end in chords:
        duration = end - start
        if name == NO_CHORD:
            diagram = "(no chord detected)"
        else:
            voicing = UKULELE_CHORDS.get(name)
            diagram = "[{}]".format(voicing) if voicing else "(no ukulele diagram available)"
        lines.append("{:<8} {:>8.2f}s - {:>8.2f}s ({:>6.2f}s)   {}".format(
            name, start, end, duration, diagram))
    return "\n".join(lines)


def format_json(chords):
    data = []
    for name, start, end in chords:
        voicing = UKULELE_CHORDS.get(name) if name != NO_CHORD else None
        data.append({
            "chord": name,
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "voicing": voicing,
        })
    return json.dumps(data, indent=2)


def _chord_at_time(chords, t):
    for label, start, end in chords:
        if start <= t < end:
            return label
    return NO_CHORD


def format_lead_sheet(pm, chords, beats_per_measure=None):
    """Simplified text lead sheet: chord symbols spaced over a measure
    grid, 4 measures per line, "." for a beat that repeats the previous
    chord and "%" for a beat with no chord detected."""
    if beats_per_measure is None:
        if pm.time_signature_changes:
            beats_per_measure = pm.time_signature_changes[0].numerator
        else:
            beats_per_measure = 4

    downbeats = list(pm.get_downbeats()) + [pm.get_end_time()]
    cells = []
    for m_start, m_end in zip(downbeats, downbeats[1:]):
        symbols, prev = [], None
        for k in range(beats_per_measure):
            t = m_start + (m_end - m_start) * k / beats_per_measure
            label = _chord_at_time(chords, t)
            symbols.append("." if label == prev else (label if label != NO_CHORD else "%"))
            prev = label
        cells.append("| " + " ".join("{:<5}".format(s) for s in symbols))

    lines = []
    for i in range(0, len(cells), 4):
        lines.append("".join(cells[i:i + 4]) + "|")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------
def run(pm, track=None, window="quarter", prompt=input):
    notes = extract_note_events(select_notes(pm, track, prompt=prompt))
    boundaries = get_window_boundaries(pm, window)
    return detect_chords(notes, boundaries)


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Convert a MIDI file into a ukulele chord chart.")
    parser.add_argument("midi_file")
    parser.add_argument("--track", default=None,
                         help="Track index, name substring (e.g. 'guitar'), or 'all' to merge every track.")
    parser.add_argument("--window", default="quarter", choices=["quarter", "half", "measure"])
    parser.add_argument("--format", dest="fmt", default="chart", choices=["chart", "json", "lead-sheet"])
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    pm = load_midi(args.midi_file)
    chords = run(pm, args.track, args.window)

    if args.fmt == "json":
        print(format_json(chords))
    elif args.fmt == "lead-sheet":
        print(format_lead_sheet(pm, chords))
    else:
        print(format_chart(chords))


if __name__ == "__main__":
    main()
