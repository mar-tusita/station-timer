"""Generate Japanese announcement voices into ../audio using Kokoro-82M (Apache-2.0).

Requires: kokoro, soundfile, numpy, and the Japanese G2P dependencies
(pyopenjtalk-plus, fugashi, jaconv, mojimoji, unidic-lite).
"""
from pathlib import Path

import numpy as np
import soundfile as sf
from kokoro import KPipeline

VOICE = "jf_alpha"
SAMPLE_RATE = 24000
LEAD_SILENCE = 0.15
PAUSE = 0.3
# Each entry is a list of phrases; PAUSE seconds of silence are inserted between them.
VOICES = {
    "start.wav": ["はじめてください"],
    "remain30.wav": ["残り30秒です"],
    "end.wav": ["時間です", "終了してください"],
}


def silence(seconds):
    return np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)


def synthesize(pipeline, phrases):
    parts = [silence(LEAD_SILENCE)]
    for i, text in enumerate(phrases):
        if i:
            parts.append(silence(PAUSE))
        parts.extend(audio for _, _, audio in pipeline(text, voice=VOICE))
    return np.concatenate(parts)


if __name__ == "__main__":
    out_dir = Path(__file__).resolve().parent.parent / "audio"
    out_dir.mkdir(exist_ok=True)
    pipeline = KPipeline(lang_code="j", repo_id="hexgrad/Kokoro-82M")
    for name, phrases in VOICES.items():
        sf.write(out_dir / name, synthesize(pipeline, phrases), SAMPLE_RATE)
        print(f"wrote {name}")
