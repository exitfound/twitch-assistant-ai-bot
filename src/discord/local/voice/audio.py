"""Speech from the TTS server into the frames Discord plays.

The server sends 16-bit mono PCM at 24 kHz; its tempo is changed here when VOICE_TEMPO
asks for it. discord.py takes 20 ms frames of 16-bit stereo at 48 kHz and encodes them
with Opus itself.
"""
import sys
import threading
from array import array

import discord
import numpy as np

# 20 ms of 48 kHz stereo 16-bit
FRAME_BYTES = 3840
SILENCE = bytes(FRAME_BYTES)
# Output bytes per second: 48000 samples × 2 channels × 2 bytes
BYTES_PER_SECOND = 192_000
# Speech gathered again after the buffer ran dry mid-answer: one pause of a second or two
# instead of a crackle of 0.5 s pieces with silence between them. Each next pause in the
# same answer gathers twice as much, up to the cap: a server that keeps lagging then gives
# a couple of pauses, not a stutter in bigger pieces
REBUFFER_SECONDS = 1.5
REBUFFER_MAX_SECONDS = 6.0
# Tempo change on 24 kHz speech: 20 ms frames overlapped by half, each placed within 5 ms
# of its nominal spot where it best continues the previous one – shorter frames blur
# the pitch, a wider search lets the tempo wander
STRETCH_HOP = 240
STRETCH_FRAME = 2 * STRETCH_HOP
STRETCH_SEEK = 120


class TimeStretch:
    """Speech sped up or slowed down without a pitch change (WSOLA), chunk by chunk.

    The output does not depend on how the network cut the input; flush() gives the rest
    once the server is done, so the answer comes out length / tempo long.
    """

    def __init__(self, tempo: float) -> None:
        self._tempo = tempo
        self._window = np.hanning(STRETCH_FRAME + 1)[:-1]
        # Input samples kept for the frames ahead, starting at absolute sample _base
        self._input = np.zeros(0)
        self._base = 0
        self._received = 0
        self._emitted = 0
        self._rest = b''
        self._frame = 0
        self._prev = 0
        # The second half of the last frame, added to the first half of the next one
        self._tail = np.zeros(STRETCH_HOP)

    def _frames(self, end: int) -> np.ndarray:
        """Every output frame the input up to absolute sample end allows."""
        out = []
        while True:
            nominal = round(self._frame * STRETCH_HOP * self._tempo)
            lo, hi = (0, 0) if self._frame == 0 else (max(0, nominal - STRETCH_SEEK), nominal + STRETCH_SEEK)
            natural = self._prev + STRETCH_HOP
            if max(hi, natural) + STRETCH_FRAME > end:
                break
            start = lo
            if hi > lo:
                ref = self._input[natural - self._base:natural - self._base + STRETCH_FRAME]
                span = self._input[lo - self._base:hi - self._base + STRETCH_FRAME]
                start += int(np.argmax(np.correlate(span, ref, 'valid')))
            frame = self._input[start - self._base:start - self._base + STRETCH_FRAME] * self._window
            out.append(self._tail + frame[:STRETCH_HOP])
            self._tail = frame[STRETCH_HOP:]
            self._prev = start
            self._frame += 1
        keep = min(round(self._frame * STRETCH_HOP * self._tempo) - STRETCH_SEEK, self._prev + STRETCH_HOP)
        if keep > self._base:
            self._input = self._input[keep - self._base:]
            self._base = keep
        return np.concatenate(out) if out else np.zeros(0)

    def _pcm(self, samples: np.ndarray) -> bytes:
        self._emitted += len(samples)
        return np.clip(np.rint(samples), -32768, 32767).astype('<i2').tobytes()

    def convert(self, pcm: bytes) -> bytes:
        if self._tempo == 1:
            return pcm
        data = self._rest + pcm
        cut = len(data) - len(data) % 2
        self._rest = data[cut:]
        samples = np.frombuffer(data[:cut], '<i2').astype(np.float64)
        self._received += len(samples)
        self._input = np.concatenate([self._input, samples])
        return self._pcm(self._frames(self._base + len(self._input)))

    def flush(self) -> bytes:
        """The rest of the answer: the input padded with silence, cut to length / tempo."""
        if self._tempo == 1:
            return b''
        # Enough silence for every frame whose nominal spot is still within the speech
        self._input = np.concatenate([self._input, np.zeros(2 * STRETCH_FRAME + 2 * STRETCH_SEEK)])
        rest = np.concatenate([self._frames(self._base + len(self._input)), self._tail])
        due = max(0, round(self._received / self._tempo) - self._emitted)
        return self._pcm(rest[:due])


class Upsampler:
    """24 kHz mono → 48 kHz stereo, chunk by chunk.

    Each input sample is preceded by the midpoint between it and the one before (linear
    interpolation), and both go to the two channels. The last sample and an odd byte
    carry over, so chunk borders – which the network sets – leave no click.
    """

    def __init__(self) -> None:
        self._prev = 0
        self._rest = b''

    def convert(self, pcm: bytes) -> bytes:
        data = self._rest + pcm
        cut = len(data) - len(data) % 2
        self._rest = data[cut:]
        samples = array('h')
        samples.frombytes(data[:cut])
        if not samples:
            return b''
        if sys.byteorder != 'little':
            samples.byteswap()
        before = [self._prev, *samples[:-1]]
        mids = array('h', [(a + b) >> 1 for a, b in zip(before, samples, strict=True)])
        self._prev = samples[-1]
        out = array('h', bytes(8 * len(samples)))
        out[0::4] = mids
        out[1::4] = mids
        out[2::4] = samples
        out[3::4] = samples
        if sys.byteorder != 'little':
            out.byteswap()
        return out.tobytes()


class StreamSource(discord.AudioSource):
    """An answer that is still being generated, played as it arrives.

    discord.py reads a frame every 20 ms from its own thread. When speech runs dry before
    the server has finished, the source goes quiet until REBUFFER_SECONDS have arrived
    again: a slow server makes one pause instead of ending the answer or crackling.
    b'' – the end – comes only after finish() and the last byte.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._finished = False
        # Frames played as silence because speech had not arrived, and the pauses they
        # made: the measure of a prebuffer that was too short
        self.underruns = 0
        self.stalls = 0
        self._rebuffering = False
        self._rebuffer_bytes = 0

    def feed(self, frames: bytes) -> None:
        with self._lock:
            self._buffer += frames

    def finish(self) -> None:
        with self._lock:
            self._finished = True

    @property
    def buffered_seconds(self) -> float:
        with self._lock:
            return len(self._buffer) / BYTES_PER_SECOND

    @property
    def empty(self) -> bool:
        with self._lock:
            return not self._buffer

    def read(self) -> bytes:
        with self._lock:
            if self._rebuffering:
                if len(self._buffer) < self._rebuffer_bytes and not self._finished:
                    self.underruns += 1
                    return SILENCE
                self._rebuffering = False
            if len(self._buffer) >= FRAME_BYTES:
                frame = bytes(self._buffer[:FRAME_BYTES])
                del self._buffer[:FRAME_BYTES]
                return frame
            if self._finished:
                if not self._buffer:
                    return b''
                frame = bytes(self._buffer).ljust(FRAME_BYTES, b'\0')
                self._buffer.clear()
                return frame
            # Ran dry mid-answer: hold one pause until a reserve has built up again
            self._rebuffering = True
            self.stalls += 1
            target = min(REBUFFER_SECONDS * 2 ** (self.stalls - 1), REBUFFER_MAX_SECONDS)
            self._rebuffer_bytes = int(target * BYTES_PER_SECOND)
            self.underruns += 1
        return SILENCE

    def is_opus(self) -> bool:
        return False
