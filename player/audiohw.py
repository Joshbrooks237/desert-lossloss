"""CoreAudio output devices and nominal sample-rate control."""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_double, c_int, c_uint32, c_uint8, c_void_p, sizeof
from dataclasses import dataclass


def _fourcc(text: str) -> int:
    return int.from_bytes(text.encode("ascii"), "big")


_COREAUDIO = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
)
_CF = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)


class _Address(ctypes.Structure):
    _fields_ = (
        ("selector", c_uint32),
        ("scope", c_uint32),
        ("element", c_uint32),
    )


class _Range(ctypes.Structure):
    _fields_ = (("minimum", c_double), ("maximum", c_double))


_COREAUDIO.AudioObjectGetPropertyData.argtypes = [
    c_uint32,
    POINTER(_Address),
    c_uint32,
    c_void_p,
    POINTER(c_uint32),
    c_void_p,
]
_COREAUDIO.AudioObjectGetPropertyData.restype = c_int
_COREAUDIO.AudioObjectGetPropertyDataSize.argtypes = [
    c_uint32,
    POINTER(_Address),
    c_uint32,
    c_void_p,
    POINTER(c_uint32),
]
_COREAUDIO.AudioObjectGetPropertyDataSize.restype = c_int
_COREAUDIO.AudioObjectSetPropertyData.argtypes = [
    c_uint32,
    POINTER(_Address),
    c_uint32,
    c_void_p,
    c_uint32,
    c_void_p,
]
_COREAUDIO.AudioObjectSetPropertyData.restype = c_int
_COREAUDIO.AudioObjectIsPropertySettable.argtypes = [
    c_uint32,
    POINTER(_Address),
    POINTER(c_uint8),
]
_COREAUDIO.AudioObjectIsPropertySettable.restype = c_int

_CF.CFStringGetCString.argtypes = [c_void_p, ctypes.c_char_p, c_int, c_uint32]
_CF.CFStringGetCString.restype = ctypes.c_bool
_CF.CFRelease.argtypes = [c_void_p]

_GLOBAL = _fourcc("glob")
_OUTPUT = _fourcc("outp")
_ELEMENT = 0
_SYSTEM = 1
_UTF8 = 0x08000100

# Clocks worth offering when a device reports a continuous range.
_LADDER = (
    44100,
    48000,
    88200,
    96000,
    176400,
    192000,
    352800,
    384000,
    705600,
    768000,
)


@dataclass
class Device:
    index: int
    audio_id: int
    name: str
    uid: str
    rate: float
    rates: list[int]


def _address(selector: int, scope: int = _GLOBAL) -> _Address:
    return _Address(selector, scope, _ELEMENT)


def _read(obj: int, selector: int, scope: int, size: int, buf) -> int:
    address = _address(selector, scope)
    io_size = c_uint32(size)
    return _COREAUDIO.AudioObjectGetPropertyData(
        obj, byref(address), 0, None, byref(io_size), buf
    )


def _byte_size(obj: int, selector: int, scope: int) -> tuple[int, int]:
    address = _address(selector, scope)
    io_size = c_uint32(0)
    err = _COREAUDIO.AudioObjectGetPropertyDataSize(
        obj, byref(address), 0, None, byref(io_size)
    )
    return err, int(io_size.value)


def _string(obj: int, selector: int) -> str:
    ref = c_void_p()
    err = _read(obj, selector, _GLOBAL, sizeof(c_void_p), byref(ref))
    if err or not ref.value:
        return ""
    raw = ctypes.create_string_buffer(1024)
    ok = _CF.CFStringGetCString(ref, raw, len(raw), _UTF8)
    _CF.CFRelease(ref)
    return raw.value.decode() if ok else ""


def _supported_rates(audio_id: int) -> list[int]:
    err, nbytes = _byte_size(audio_id, _fourcc("nsr#"), _GLOBAL)
    if err or nbytes <= 0:
        return []
    count = nbytes // sizeof(_Range)
    ranges = (_Range * count)()
    if _read(audio_id, _fourcc("nsr#"), _GLOBAL, nbytes, ranges):
        return []
    values: list[int] = []
    for item in ranges:
        if abs(item.minimum - item.maximum) < 0.5:
            values.append(int(round(item.minimum)))
            continue
        for candidate in _LADDER:
            if item.minimum - 1 <= candidate <= item.maximum + 1:
                values.append(candidate)
    unique: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def output_devices() -> list[Device]:
    """Output-capable devices."""
    err, nbytes = _byte_size(_SYSTEM, _fourcc("dev#"), _GLOBAL)
    if err or nbytes <= 0:
        return []
    count = nbytes // 4
    ids = (c_uint32 * count)()
    if _read(_SYSTEM, _fourcc("dev#"), _GLOBAL, nbytes, ids):
        return []
    devices: list[Device] = []
    for index, audio_id in enumerate(ids):
        err, stream_bytes = _byte_size(int(audio_id), _fourcc("stm#"), _OUTPUT)
        if err or stream_bytes < 4:
            continue
        rate = c_double()
        _read(int(audio_id), _fourcc("nsrt"), _GLOBAL, sizeof(c_double), byref(rate))
        devices.append(
            Device(
                index=index,
                audio_id=int(audio_id),
                name=_string(int(audio_id), _fourcc("lnam")) or f"Device {index}",
                uid=_string(int(audio_id), _fourcc("uid ")),
                rate=float(rate.value),
                rates=_supported_rates(int(audio_id)),
            )
        )
    return devices


def default_output_uid() -> str:
    audio_id = c_uint32()
    err = _read(_SYSTEM, _fourcc("dOut"), _GLOBAL, sizeof(c_uint32), byref(audio_id))
    if err:
        return ""
    return _string(int(audio_id.value), _fourcc("uid "))


def read_rate(audio_id: int) -> float:
    rate = c_double()
    err = _read(audio_id, _fourcc("nsrt"), _GLOBAL, sizeof(c_double), byref(rate))
    if err:
        raise OSError(f"could not read the device clock ({err})")
    return float(rate.value)


def set_rate(audio_id: int, rate: float) -> None:
    address = _address(_fourcc("nsrt"))
    settable = c_uint8(0)
    err = _COREAUDIO.AudioObjectIsPropertySettable(
        audio_id, byref(address), byref(settable)
    )
    if err or not settable.value:
        raise OSError("this device will not change its clock")
    value = c_double(float(rate))
    err = _COREAUDIO.AudioObjectSetPropertyData(
        audio_id, byref(address), 0, None, sizeof(value), byref(value)
    )
    if err:
        raise OSError(f"could not set the device clock ({err})")


def nearest_rate(rates: list[int], wanted: int) -> int | None:
    if not rates:
        return None
    if wanted in rates:
        return wanted
    below = [rate for rate in rates if rate <= wanted]
    if below:
        return max(below)
    return min(rates)
