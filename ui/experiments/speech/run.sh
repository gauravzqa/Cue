#!/bin/bash
# Stage 0b, test #2: is macOS 26's SpeechTranscriber good enough to replace the
# LocalTranscriber stub and delete silero-vad + torch + sounddevice?
#
# Synthesises the 40 utterances in evals/address_gate_cases.jsonl with `say`,
# transcribes each one through SpeechAnalyzer + SpeechTranscriber, and scores
# WER and exact-match against the labels.
#
# Transcribing a FILE (rather than the live microphone) needs no TCC grant and
# raises no permission dialog. The on-device model assets install themselves on
# first run.
#
#   ./run.sh [voice] [rate]        e.g.  ./run.sh Alex 195
#
set -euo pipefail
cd "$(dirname "$0")"

VOICE="${1:-Samantha}"
RATE="${2:-175}"
WORK="${DAA_SPEECH_WORK:-$(mktemp -d)}"
CASES="../../../evals/address_gate_cases.jsonl"

echo "==> synthesising 40 utterances with '$VOICE' at ${RATE}wpm into $WORK"
mkdir -p "$WORK/audio"
python3 - "$WORK" "$VOICE" "$RATE" "$CASES" <<'PY'
import json, os, subprocess, sys
work, voice, rate, cases = sys.argv[1:5]
rows = [json.loads(l) for l in open(cases)]
manifest = []
for i, r in enumerate(rows):
    wav = f"{work}/audio/{i:02d}.wav"
    manifest.append({"i": i, "ref": r["utterance"], "wav": wav,
                     "category": r["category"]})
    subprocess.run(["say", "-v", voice, "-r", rate,
                    "--data-format=LEF32@16000", "-o", wav, r["utterance"]],
                   check=True)
json.dump(manifest, open(work + "/manifest.json", "w"), indent=1)
print(f"    {len(manifest)} files")
PY

echo "==> building the probe"
swiftc -sdk "$(xcrun --sdk macosx --show-sdk-path)" \
       -target arm64-apple-macos26.0 -parse-as-library -swift-version 6 \
       probe.swift -o "$WORK/probe"

echo "==> transcribing"
"$WORK/probe" "$WORK"/audio/*.wav > "$WORK/results.json"

echo "==> scoring"
DAA_SPEECH_WORK="$WORK" python3 score.py "$WORK/results.json"
