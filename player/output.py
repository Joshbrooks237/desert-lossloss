"""Playback. ffmpeg sends raw frames. The queue applies the level.

The gain lives on the output, not in the decoder. Audio already handed to the
device changes level immediately, which is what the knob is for.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import (
    POINTER,
    byref,
    c_double,
    c_float,
    c_int,
    c_int16,
    c_uint32,
    c_uint64,
    c_void_p,
)


_BUFFER_FRAMES = 2048
_BUFFER_COUNT = 3

_FORMATS = {
    "pcm_s16le": (16, False),
    "pcm_s32le": (32, False),
    "pcm_f32le": (32, True),
}


def open_output(rate: int, channels: int, codec: str, device_uid: str, volume: float):
    if sys.platform == "darwin":
        return MacOutput(rate, channels, codec, device_uid, volume)
    if sys.platform == "win32":
        from output_win import WinOutput

        return WinOutput(rate, channels, codec, device_uid, volume)
    raise OSError("LOSSLOSS plays on macOS and Windows.")


class MacOutput:
    def __init__(self, rate: int, channels: int, codec: str, device_uid: str, volume: float) -> None:
        bits, is_float = _FORMATS.get(codec, (0, False))
        if bits <= 0 or rate <= 0 or channels <= 0:
            raise OSError("This file has no playable format.")
        self._rate = int(rate)
        self._channels = int(channels)
        self._bits = bits
        self._is_float = is_float
        self._device_uid = device_uid
        self._frame_bytes = channels * (bits // 8)
        self._buffer_bytes = _BUFFER_FRAMES * self._frame_bytes
        self._high_water = max(self._buffer_bytes * _BUFFER_COUNT * 2, self._rate * self._frame_bytes)
        self._pcm = bytearray()
        self._volume = min(1.0, max(0.0, float(volume)))
        self._aq = c_void_p()
        self._started = False
        self._closed = False
        self._eof = False
        self._drained = False
        self._in_flight = 0
        self._in_callback = 0
        self._last_seconds = 0.0
        self._failure = ""
        self._lock = threading.Lock()
        self._api = threading.Lock()
        self._id = _register(self)
        self._callback = _OUTPUT_CALLBACK(_queue_callback)

    def write(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            failure = ""
            start = False
            with self._lock:
                if self._closed:
                    return
                failure = self._failure
                room = self._high_water - len(self._pcm)
                if not failure and room > 0:
                    chunk = data[offset : offset + room]
                    self._pcm.extend(chunk)
                    offset += len(chunk)
                    start = not self._started and len(self._pcm) >= self._buffer_bytes * _BUFFER_COUNT
            if failure:
                raise OSError(failure)
            if start:
                self._start()
            elif offset < len(data):
                time.sleep(0.008)

    def finish(self) -> None:
        with self._lock:
            self._eof = True
            failure = self._failure
            start = not self._started and len(self._pcm) > 0
            empty = not self._started and not self._pcm
        if failure:
            raise OSError(failure)
        if empty:
            with self._lock:
                self._drained = True
            return
        if start:
            self._start()

    def set_volume(self, volume: float) -> None:
        volume = min(1.0, max(0.0, float(volume)))
        with self._api:
            with self._lock:
                self._volume = volume
                queue = self._queue()
            if queue:
                _apply_volume(queue, volume)

    def seconds(self) -> float:
        with self._api:
            with self._lock:
                queue = self._queue()
                rate = self._rate
                last = self._last_seconds
            if not queue or rate <= 0:
                return last
            stamp = _TimeStamp()
            err = _AQ.AudioQueueGetCurrentTime(queue, None, byref(stamp), None)
        if err or not (stamp.flags & 1):
            return last
        heard = max(0.0, float(stamp.sample_time) / rate)
        with self._lock:
            self._last_seconds = heard
        return heard

    def drained(self) -> bool:
        with self._lock:
            return self._drained or bool(self._failure)

    def close(self) -> None:
        with self._api:
            with self._lock:
                self._closed = True
                self._drained = True
                queue = self._aq.value or None
                self._aq = c_void_p()
            while True:
                with self._lock:
                    busy = self._in_callback
                if not busy:
                    break
                time.sleep(0.002)
            if queue:
                _AQ.AudioQueueStop(queue, 1)
                _AQ.AudioQueueDispose(queue, 1)
        _forget(self._id)

    def _queue(self):
        # Caller holds self._lock.
        if self._closed or not self._aq.value:
            return None
        return self._aq

    def _start(self) -> None:
        with self._lock:
            if self._started or self._closed:
                return
            self._started = True
        try:
            with self._api:
                with self._lock:
                    if self._closed:
                        return
                self._open_and_run()
        except OSError as exc:
            with self._lock:
                self._failure = str(exc)
                self._drained = True
            raise

    def _open_and_run(self) -> None:
        queue = self._create_queue()
        try:
            chunks = self._prime_chunks()
            buffers = []
            for chunk in chunks:
                audio_buffer = POINTER(_QueueBuffer)()
                err = _AQ.AudioQueueAllocateBuffer(queue, self._buffer_bytes, byref(audio_buffer))
                _check(err, "could not allocate a playback buffer")
                ctypes.memmove(audio_buffer.contents.data, chunk, len(chunk))
                audio_buffer.contents.size = len(chunk)
                buffers.append(audio_buffer)
            with self._lock:
                if self._closed:
                    _AQ.AudioQueueDispose(queue, 1)
                    return
                self._aq = queue
                self._in_flight = len(buffers)
                volume = self._volume
            for audio_buffer in buffers:
                err = _AQ.AudioQueueEnqueueBuffer(queue, audio_buffer, 0, None)
                _check(err, "could not queue audio")
            _apply_volume(queue, volume)
            err = _AQ.AudioQueueStart(queue, None)
            _check(err, "the output device rejected this format")
            with self._lock:
                volume = self._volume
            _apply_volume(queue, volume)
        except Exception:
            with self._lock:
                if self._aq.value == (queue.value if queue else 0):
                    self._aq = c_void_p()
            _AQ.AudioQueueDispose(queue, 1)
            raise

    def _create_queue(self) -> c_void_p:
        sample_bytes = self._bits // 8
        flags = 0x1 | 0x8 if self._is_float else 0x4 | 0x8
        description = _StreamFormat(
            self._rate,
            _fourcc("lpcm"),
            flags,
            self._frame_bytes,
            1,
            self._frame_bytes,
            self._channels,
            self._bits,
            0,
        )
        queue = c_void_p()
        err = _AQ.AudioQueueNewOutput(
            byref(description),
            self._callback,
            c_void_p(self._id),
            None,
            None,
            0,
            byref(queue),
        )
        _check(err, "could not open the output")
        if self._device_uid:
            text = _CF.CFStringCreateWithCString(None, self._device_uid.encode(), 0x08000100)
            if not text:
                _AQ.AudioQueueDispose(queue, 1)
                raise OSError("could not select that output")
            try:
                err = _AQ.AudioQueueSetProperty(queue, _fourcc("aqcd"), byref(c_void_p(text)), 8)
            finally:
                _CF.CFRelease(text)
            if err:
                _AQ.AudioQueueDispose(queue, 1)
                raise OSError("that output will not take the stream")
        return queue

    def _prime_chunks(self) -> list[bytes]:
        chunks: list[bytes] = []
        for _ in range(_BUFFER_COUNT):
            chunk = self._take(self._buffer_bytes, allow_silence=False)
            if not chunk:
                break
            chunks.append(chunk)
        if not chunks:
            raise OSError("nothing to play")
        return chunks

    def _take(self, capacity: int, allow_silence: bool) -> bytes | None:
        frame = self._frame_bytes
        with self._lock:
            if self._closed:
                return None
            usable = len(self._pcm) - (len(self._pcm) % frame)
            if usable <= 0:
                if self._eof or not allow_silence:
                    return None
                silence = capacity - (capacity % frame)
                return b"\x00" * silence if silence else None
            take = min(capacity, usable)
            take -= take % frame
            if take <= 0:
                return None
            data = bytes(self._pcm[:take])
            del self._pcm[:take]
            return data

    def _on_buffer(self, audio_buffer) -> None:
        with self._lock:
            self._in_callback += 1
            queue = self._queue()
        try:
            if queue is None:
                return
            data = self._take(audio_buffer.contents.capacity, allow_silence=True)
            if not data:
                with self._lock:
                    self._in_flight -= 1
                    if self._in_flight <= 0:
                        self._drained = True
                return
            ctypes.memmove(audio_buffer.contents.data, data, len(data))
            audio_buffer.contents.size = len(data)
            err = _AQ.AudioQueueEnqueueBuffer(queue, audio_buffer, 0, None)
            if err:
                with self._lock:
                    self._failure = f"playback stopped ({err})"
                    self._drained = True
                    self._in_flight -= 1
        finally:
            with self._lock:
                self._in_callback -= 1


def _fourcc(text: str) -> int:
    return int.from_bytes(text.encode("ascii"), "big")


def _check(err: int, message: str) -> None:
    if err:
        raise OSError(f"{message} ({err})")


def _apply_volume(queue, volume: float) -> None:
    # Ramp of zero so the new gain hits audio already in the queue.
    _AQ.AudioQueueSetParameter(queue, 4, c_float(0))
    _AQ.AudioQueueSetParameter(queue, 1, c_float(volume))


class _StreamFormat(ctypes.Structure):
    _fields_ = (
        ("rate", c_double),
        ("format_id", c_uint32),
        ("flags", c_uint32),
        ("bytes_per_packet", c_uint32),
        ("frames_per_packet", c_uint32),
        ("bytes_per_frame", c_uint32),
        ("channels", c_uint32),
        ("bits", c_uint32),
        ("reserved", c_uint32),
    )


class _Smpte(ctypes.Structure):
    _fields_ = (
        ("subframes", c_int16),
        ("divisor", c_int16),
        ("counter", c_uint32),
        ("kind", c_uint32),
        ("flags", c_uint32),
        ("hours", c_int16),
        ("minutes", c_int16),
        ("seconds", c_int16),
        ("frames", c_int16),
    )


class _TimeStamp(ctypes.Structure):
    _fields_ = (
        ("sample_time", c_double),
        ("host_time", c_uint64),
        ("rate_scalar", c_double),
        ("word_clock", c_uint64),
        ("smpte", _Smpte),
        ("flags", c_uint32),
        ("reserved", c_uint32),
    )


class _QueueBuffer(ctypes.Structure):
    _fields_ = (
        ("capacity", c_uint32),
        ("_pad0", c_uint32),
        ("data", c_void_p),
        ("size", c_uint32),
        ("_pad1", c_uint32),
        ("user", c_void_p),
        ("packet_capacity", c_uint32),
        ("_pad2", c_uint32),
        ("packets", c_void_p),
        ("packet_count", c_uint32),
        ("_pad3", c_uint32),
    )


_OUTPUT_CALLBACK = ctypes.CFUNCTYPE(None, c_void_p, c_void_p, POINTER(_QueueBuffer))


def _queue_callback(user, _queue, audio_buffer) -> None:
    try:
        key = int(user or 0)
    except (TypeError, ValueError):
        return
    with _owners_lock:
        owner = _owners.get(key)
    if owner is None:
        return
    try:
        owner._on_buffer(audio_buffer)
    except Exception:
        return


_owners_lock = threading.Lock()
_owners: dict[int, MacOutput] = {}
_next_owner = 1


def _register(owner: MacOutput) -> int:
    global _next_owner
    with _owners_lock:
        key = _next_owner
        _next_owner += 1
        _owners[key] = owner
        return key


def _forget(key: int) -> None:
    with _owners_lock:
        _owners.pop(key, None)


if sys.platform == "darwin":
    _AQ = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox")
    _CF = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

    _AQ.AudioQueueNewOutput.argtypes = [
        POINTER(_StreamFormat),
        _OUTPUT_CALLBACK,
        c_void_p,
        c_void_p,
        c_void_p,
        c_uint32,
        POINTER(c_void_p),
    ]
    _AQ.AudioQueueNewOutput.restype = c_int
    _AQ.AudioQueueAllocateBuffer.argtypes = [c_void_p, c_uint32, POINTER(POINTER(_QueueBuffer))]
    _AQ.AudioQueueAllocateBuffer.restype = c_int
    _AQ.AudioQueueEnqueueBuffer.argtypes = [c_void_p, POINTER(_QueueBuffer), c_uint32, c_void_p]
    _AQ.AudioQueueEnqueueBuffer.restype = c_int
    _AQ.AudioQueueStart.argtypes = [c_void_p, c_void_p]
    _AQ.AudioQueueStart.restype = c_int
    _AQ.AudioQueueStop.argtypes = [c_void_p, c_uint32]
    _AQ.AudioQueueStop.restype = c_int
    _AQ.AudioQueueDispose.argtypes = [c_void_p, c_uint32]
    _AQ.AudioQueueDispose.restype = c_int
    _AQ.AudioQueueSetParameter.argtypes = [c_void_p, c_uint32, c_float]
    _AQ.AudioQueueSetParameter.restype = c_int
    _AQ.AudioQueueSetProperty.argtypes = [c_void_p, c_uint32, c_void_p, c_uint32]
    _AQ.AudioQueueSetProperty.restype = c_int
    _AQ.AudioQueueGetCurrentTime.argtypes = [c_void_p, c_void_p, POINTER(_TimeStamp), c_void_p]
    _AQ.AudioQueueGetCurrentTime.restype = c_int
    _CF.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, c_uint32]
    _CF.CFStringCreateWithCString.restype = c_void_p
    _CF.CFRelease.argtypes = [c_void_p]
    _CF.CFRelease.restype = None
else:
    _AQ = None
    _CF = None
