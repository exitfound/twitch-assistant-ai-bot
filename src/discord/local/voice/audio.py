"""Speech from the TTS server into the frames Discord plays.

The server sends 16-bit mono PCM at 24 kHz; discord.py takes 20 ms frames of 16-bit
stereo at 48 kHz and encodes them with Opus itself.
"""
import sys
import threading
from array import array

import discord

# 20 ms of 48 kHz stereo 16-bit
FRAME_BYTES = 3840
SILENCE = bytes(FRAME_BYTES)
# Output bytes per second: 48000 samples × 2 channels × 2 bytes
BYTES_PER_SECOND = 192_000
# Speech gathered again after the buffer ran dry mid-answer: one pause of a second or two
# instead of a crackle of 0.5 s pieces with silence between them
REBUFFER_SECONDS = 1.5


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
                if len(self._buffer) < REBUFFER_SECONDS * BYTES_PER_SECOND and not self._finished:
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
            self.underruns += 1
        return SILENCE

    def is_opus(self) -> bool:
        return False
