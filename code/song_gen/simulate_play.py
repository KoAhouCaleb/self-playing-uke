"""
simulate_play.py
=================

Runs the midi_to_uke pipeline on a MIDI file, synthesizes a simple
strummed-ukulele rendition of the detected chord chart, and exports it
as an mp3 so the chart can be sanity-checked by ear against the source
track. Requires the `ffmpeg` binary on PATH for the mp3 encode step.

Usage
-----
    python simulate_play.py song.mid -o song_uke.mp3
    python simulate_play.py song.mid --track guitar --window measure
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import midi_to_uke as mtu

# ---------------------------------------------------------------------------
# Synthesis parameters
# ---------------------------------------------------------------------------
STRUM_STAGGER_SECONDS = 0.015   # onset offset between successive strings in a strum
RING_SECONDS = 2.5              # max natural decay length of a single plucked string
STRING_GAIN = 0.22              # per-string amplitude before final normalization
# (harmonic number, relative amplitude, exponential decay rate in 1/s) -- a
# few decaying harmonics stacked together give a rough plucked-string timbre
HARMONICS = [(1, 1.0, 3.0), (2, 0.5, 4.0), (3, 0.3, 5.5), (4, 0.15, 7.0)]


def pluck_tone(freq, duration, sample_rate):
    t = np.arange(int(duration * sample_rate), dtype=np.float32) / sample_rate
    tone = np.zeros_like(t)
    for harmonic, amp, decay in HARMONICS:
        tone += amp * np.sin(2 * np.pi * freq * harmonic * t) * np.exp(-decay * t)
    return tone.astype(np.float32)


def synthesize(chords, sample_rate, strum_stagger=STRUM_STAGGER_SECONDS, ring_seconds=RING_SECONDS):
    total_duration = chords[-1][2] if chords else 0.0
    total_samples = int((total_duration + ring_seconds + 1.0) * sample_rate)
    buffer = np.zeros(total_samples, dtype=np.float32)

    unresolved = set()
    for name, start, end in chords:
        if name == mtu.NO_CHORD:
            continue
        voicing = mtu.UKULELE_CHORDS.get(name)
        if not voicing:
            unresolved.add(name)
            continue

        span = max(end - start, 0.05)
        ring = min(ring_seconds, span + 1.0)
        for string_idx in range(4):
            fret = int(voicing[string_idx])
            pitch = mtu.UKULELE_OPEN_MIDI_PITCHES[string_idx] + fret
            freq = 440.0 * (2.0 ** ((pitch - 69) / 12.0))

            onset_sample = int((start + string_idx * strum_stagger) * sample_rate)
            if onset_sample >= len(buffer):
                continue
            tone = pluck_tone(freq, ring, sample_rate)
            end_sample = min(onset_sample + len(tone), len(buffer))
            tone = tone[: end_sample - onset_sample]
            buffer[onset_sample:end_sample] += tone * STRING_GAIN

    return buffer, unresolved


def write_wav(buffer, sample_rate, path):
    peak = float(np.max(np.abs(buffer))) if buffer.size else 0.0
    normalized = buffer / peak * 0.9 if peak > 0 else buffer
    samples = (normalized * 32767.0).astype(np.int16)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())


def convert_to_mp3(wav_path, mp3_path):
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not found on PATH -- install it (e.g. `apt install ffmpeg`) to export mp3.")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path,
         "-codec:a", "libmp3lame", "-qscale:a", "2", mp3_path],
        check=True,
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Render a MIDI file's detected ukulele chord chart as an mp3.")
    parser.add_argument("midi_file")
    parser.add_argument("-o", "--output", default=None,
                         help="Output mp3 path (default: <midi file stem>_uke.mp3 in the current directory).")
    parser.add_argument("--track", default="all",
                         help="Track index, name substring (e.g. 'guitar'), or 'all' (default) to merge every track.")
    parser.add_argument("--window", default="half", choices=["quarter", "half", "measure"])
    parser.add_argument("--sample-rate", type=int, default=44100)
    parser.add_argument("--quiet", action="store_true", help="Don't print the chord chart to stdout.")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    pm = mtu.load_midi(args.midi_file)
    chords = mtu.run(pm, args.track, args.window)

    if not args.quiet:
        print(mtu.format_chart(chords))

    no_chord_count = sum(1 for name, _, _ in chords if name == mtu.NO_CHORD)
    print("\n{} chord span(s) detected, {} flagged as no-chord.".format(len(chords), no_chord_count),
          file=sys.stderr)

    buffer, unresolved = synthesize(chords, args.sample_rate)
    if unresolved:
        print("No ukulele diagram available for: {} (left silent)".format(", ".join(sorted(unresolved))),
              file=sys.stderr)

    output = args.output or (os.path.splitext(os.path.basename(args.midi_file))[0] + "_uke.mp3")
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        write_wav(buffer, args.sample_rate, wav_path)
        convert_to_mp3(wav_path, output)
    finally:
        os.remove(wav_path)

    print("Wrote {}".format(output))


if __name__ == "__main__":
    main()
