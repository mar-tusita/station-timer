"""Generate bell sounds (bell1.wav, bell2.wav, bell3.wav) into ../audio using only the standard library."""
import math
import struct
import wave
from pathlib import Path

RATE = 44100
BASE_FREQ = 1850.0
# (frequency ratio, amplitude, decay rate) for a desk-bell-like inharmonic spectrum
PARTIALS = [(1.0, 1.0, 4.0), (2.32, 0.5, 6.0), (4.25, 0.3, 10.0), (6.63, 0.15, 16.0)]
STRIKE_SECONDS = 1.2
INTERVAL = 0.4


def strike(length):
    n = int(RATE * length)
    out = []
    for i in range(n):
        t = i / RATE
        v = sum(a * math.exp(-d * t) * math.sin(2 * math.pi * BASE_FREQ * r * t) for r, a, d in PARTIALS)
        attack = min(1.0, t / 0.003)
        out.append(v * attack)
    return out


def render(count):
    total = int(RATE * INTERVAL * (count - 1)) + int(RATE * STRIKE_SECONDS)
    buf = [0.0] * total
    one = strike(STRIKE_SECONDS)
    for k in range(count):
        offset = int(RATE * INTERVAL * k)
        for i, v in enumerate(one):
            buf[offset + i] += v
    peak = max(abs(v) for v in buf)
    return [v / peak * 0.8 for v in buf]


def write(path, samples):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(v * 32767)) for v in samples))


if __name__ == "__main__":
    out_dir = Path(__file__).resolve().parent.parent / "audio"
    out_dir.mkdir(exist_ok=True)
    for n in (1, 2, 3):
        write(out_dir / f"bell{n}.wav", render(n))
        print(f"wrote bell{n}.wav")
