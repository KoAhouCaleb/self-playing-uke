"""End-to-end test: build a tiny synthetic MIDI (C major triad held for
2 beats, then G major triad for 2 beats) and confirm the pipeline
detects the expected chords, timings, and ukulele voicings."""

import os
import sys

import pretty_midi

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import midi_to_uke as mtu

TEMPO_BPM = 120.0
BEAT_SECONDS = 60.0 / TEMPO_BPM


def build_synthetic_midi():
    pm = pretty_midi.PrettyMIDI(initial_tempo=TEMPO_BPM)
    guitar = pretty_midi.Instrument(program=24, name="Nylon Guitar")

    # C major triad (C4, E4, G4) for 2 beats
    for pitch in (60, 64, 67):
        guitar.notes.append(pretty_midi.Note(
            velocity=90, pitch=pitch, start=0.0, end=2 * BEAT_SECONDS))

    # G major triad (G3, B3, D4) for the next 2 beats
    for pitch in (55, 59, 62):
        guitar.notes.append(pretty_midi.Note(
            velocity=90, pitch=pitch, start=2 * BEAT_SECONDS, end=4 * BEAT_SECONDS))

    pm.instruments.append(guitar)
    return pm


def test_pipeline_detects_c_then_g():
    pm = build_synthetic_midi()
    chords = mtu.run(pm, track=None, window="quarter", prompt=lambda _: "0")

    names = [c[0] for c in chords]
    assert names == ["C", "G"], names

    c_name, c_start, c_end = chords[0]
    g_name, g_start, g_end = chords[1]

    assert c_start == 0.0
    assert abs(c_end - 2 * BEAT_SECONDS) < 1e-6
    assert abs(g_start - 2 * BEAT_SECONDS) < 1e-6
    assert abs(g_end - 4 * BEAT_SECONDS) < 1e-6


def test_ukulele_voicings_match_known_shapes():
    # These are the canonical open-position GCEA shapes.
    assert mtu.UKULELE_CHORDS["C"] == "0003"
    assert mtu.UKULELE_CHORDS["G"] == "0232"
    assert mtu.UKULELE_CHORDS["Am"] == "2000"


def test_track_selection_by_name_and_all():
    pm = build_synthetic_midi()
    by_name = mtu.select_notes(pm, "guitar")
    by_all = mtu.select_notes(pm, "all")
    assert len(by_name) == 6
    assert len(by_all) == 6


def test_sparse_window_flagged_as_no_chord():
    pm = pretty_midi.PrettyMIDI(initial_tempo=TEMPO_BPM)
    inst = pretty_midi.Instrument(program=24, name="guitar")
    # a single melody note -- not a chord
    inst.notes.append(pretty_midi.Note(velocity=90, pitch=64, start=0.0, end=1 * BEAT_SECONDS))
    pm.instruments.append(inst)

    chords = mtu.run(pm, track=None, window="quarter")
    assert all(name == mtu.NO_CHORD for name, _, _ in chords)


def test_format_json_round_trips_chord_and_voicing():
    pm = build_synthetic_midi()
    chords = mtu.run(pm, track="guitar", window="quarter")
    import json
    data = json.loads(mtu.format_json(chords))
    assert data[0]["chord"] == "C"
    assert data[0]["voicing"] == "0003"
    assert data[1]["chord"] == "G"
    assert data[1]["voicing"] == "0232"


if __name__ == "__main__":
    test_pipeline_detects_c_then_g()
    test_ukulele_voicings_match_known_shapes()
    test_track_selection_by_name_and_all()
    test_sparse_window_flagged_as_no_chord()
    test_format_json_round_trips_chord_and_voicing()
    print("All tests passed.")
