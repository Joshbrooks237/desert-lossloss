"""Playback engine. ffmpeg decodes. The output queue plays and keeps the clock."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import audiohw
import output

AUDIO_EXT = {
    ".aac",
    ".aif",
    ".aifc",
    ".aiff",
    ".alac",
    ".ape",
    ".caf",
    ".dff",
    ".dsf",
    ".flac",
    ".m4a",
    ".mka",
    ".mp3",
    ".mp4",
    ".oga",
    ".ogg",
    ".opus",
    ".tta",
    ".wav",
    ".wave",
    ".wma",
    ".wv",
}

LOSSLESS_CODECS = {
    "alac",
    "ape",
    "flac",
    "pcm_f32be",
    "pcm_f32le",
    "pcm_f64be",
    "pcm_f64le",
    "pcm_s16be",
    "pcm_s16le",
    "pcm_s24be",
    "pcm_s24le",
    "pcm_s32be",
    "pcm_s32le",
    "pcm_u8",
    "tta",
    "wavpack",
}

LOSSY_CODECS = {
    "aac",
    "ac3",
    "mp2",
    "mp3",
    "opus",
    "vorbis",
    "wmav1",
    "wmav2",
}

SETTINGS = Path.home() / "Library" / "Application Support" / "lossloss" / "settings.json"


def _arch() -> str:
    return "arm64" if platform.machine() == "arm64" else "x86_64"


def _tool(name: str) -> str:
    bundled = Path(__file__).resolve().parent.parent / "bin" / _arch() / name
    if os.access(bundled, os.X_OK):
        return str(bundled)
    found = shutil.which(name)
    if found:
        return found
    for folder in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        candidate = os.path.join(folder, name)
        if os.access(candidate, os.X_OK):
            return candidate
    return name


FFMPEG = _tool("ffmpeg")
FFPROBE = _tool("ffprobe")


@dataclass
class Track:
    id: int
    path: str
    title: str
    artist: str
    album: str
    track_no: int
    disc_no: int
    duration: float
    codec: str
    sample_rate: int
    bits: int
    channels: int
    layout: str
    bitrate: int
    grade: str
    has_art: bool

    def public(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "track_no": self.track_no,
            "disc_no": self.disc_no,
            "duration": self.duration,
            "codec": self.codec,
            "sample_rate": self.sample_rate,
            "bits": self.bits,
            "channels": self.channels,
            "layout": self.layout,
            "bitrate": self.bitrate,
            "grade": self.grade,
            "has_art": self.has_art,
        }


@dataclass
class Plan:
    device_index: int
    codec: str
    resample_to: int | None
    clock: float | None
    delivery: str
    advice: str


def ffmpeg_ready() -> str | None:
    if os.access(FFMPEG, os.X_OK) and os.access(FFPROBE, os.X_OK):
        return None
    return "ffmpeg and ffprobe need to be installed."


def _leading_int(value: str) -> int:
    digits: list[str] = []
    for char in value.strip():
        if char.isdigit():
            digits.append(char)
        elif digits:
            break
    return int("".join(digits)) if digits else 0


def _tag(tags: dict, *names: str) -> str:
    lowered = {str(key).lower(): value for key, value in tags.items() if value}
    for name in names:
        value = lowered.get(name)
        if value:
            return str(value).strip()
    return ""


def _bits(stream: dict) -> int:
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        try:
            value = int(stream.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value
    fmt = str(stream.get("sample_fmt") or "")
    for width in (64, 32, 24, 16, 8):
        if str(width) in fmt:
            return width
    return 0


def _grade(codec: str, bits: int, sample_rate: int, suffix: str) -> str:
    if codec.startswith("dsd") or suffix in {".dsf", ".dff"}:
        return "dsd"
    lossless = codec in LOSSLESS_CODECS
    if not lossless and codec not in LOSSY_CODECS:
        # Unknown PCM-ish wav/aiff variants still count when the name says pcm.
        lossless = codec.startswith("pcm_")
    if lossless and (bits >= 24 or sample_rate >= 88200):
        return "hires"
    if lossless:
        return "lossless"
    return "lossy"


def probe(path: str, track_id: int) -> Track | None:
    try:
        result = subprocess.run(
            [
                FFPROBE,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        data = json.loads(result.stdout or "{}")
    except (OSError, json.JSONDecodeError):
        return None
    streams = data.get("streams") or []
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if audio is None:
        return None
    fmt = data.get("format") or {}
    tags = {}
    tags.update(audio.get("tags") or {})
    tags.update(fmt.get("tags") or {})
    suffix = Path(path).suffix.lower()
    codec = str(audio.get("codec_name") or "unknown")
    try:
        sample_rate = int(audio.get("sample_rate") or 0)
    except (TypeError, ValueError):
        sample_rate = 0
    bits = _bits(audio)
    channels = int(audio.get("channels") or 0)
    layout = str(audio.get("channel_layout") or "")
    if not layout:
        layout = {1: "mono", 2: "stereo"}.get(channels, f"{channels}ch" if channels else "")
    try:
        duration = float(fmt.get("duration") or audio.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    try:
        bitrate = int(fmt.get("bit_rate") or audio.get("bit_rate") or 0)
    except (TypeError, ValueError):
        bitrate = 0
    title = _tag(tags, "title") or Path(path).stem
    has_art = any(
        stream.get("codec_type") == "video"
        or (stream.get("disposition") or {}).get("attached_pic")
        for stream in streams
    )
    return Track(
        id=track_id,
        path=path,
        title=title,
        artist=_tag(tags, "artist", "album_artist"),
        album=_tag(tags, "album"),
        track_no=_leading_int(_tag(tags, "track")),
        disc_no=_leading_int(_tag(tags, "disc")),
        duration=duration,
        codec=codec,
        sample_rate=sample_rate,
        bits=bits,
        channels=channels,
        layout=layout,
        bitrate=bitrate,
        grade=_grade(codec, bits, sample_rate, suffix),
        has_art=has_art,
    )


def extract_art(path: str) -> bytes | None:
    try:
        result = subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "0",
                "-i",
                path,
                "-an",
                "-map",
                "0:v:0",
                "-c:v",
                "mjpeg",
                "-vframes",
                "1",
                "-f",
                "image2pipe",
                "pipe:1",
            ],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout


def _khz(rate: float | int) -> str:
    value = int(round(float(rate)))
    special = {
        44100: "44.1",
        48000: "48",
        88200: "88.2",
        96000: "96",
        176400: "176.4",
        192000: "192",
        352800: "352.8",
        384000: "384",
        705600: "705.6",
        768000: "768",
    }
    if value in special:
        return special[value]
    text = f"{value / 1000:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _collect(paths: list[str]) -> list[str]:
    files: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            for root, dirs, names in os.walk(path):
                dirs.sort()
                for name in sorted(names):
                    if Path(name).suffix.lower() in AUDIO_EXT:
                        files.append(str(Path(root) / name))
        elif path.is_file():
            files.append(str(path))
    return files


class Engine:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.queue: list[Track] = []
        self.index = -1
        self.status = "stopped"  # stopped | playing | paused
        self.position = 0.0
        self.volume = 1.0
        self.match_rate = True
        self.device_uid = ""
        self.error = ""
        self.notice = ""
        self.scanning = False
        self.scan_label = ""
        self._ids = 0
        self._gen = 0
        self._proc: subprocess.Popen | None = None
        self._player: output.MacOutput | None = None
        self._seek = 0.0
        self._saved_rates: dict[int, float] = {}
        self._art: dict[int, bytes] = {}
        self._save_timer: threading.Timer | None = None
        self._load_settings()

    def _load_settings(self) -> None:
        try:
            data = json.loads(SETTINGS.read_text())
        except (OSError, json.JSONDecodeError):
            return
        try:
            self.volume = min(1.0, max(0.0, float(data.get("volume", 1))))
        except (TypeError, ValueError):
            self.volume = 1.0
        self.match_rate = bool(data.get("match_rate", True))
        self.device_uid = str(data.get("device_uid") or "")

    def _save_settings_soon(self) -> None:
        with self.lock:
            timer = self._save_timer
            self._save_timer = None
        if timer is not None:
            timer.cancel()
        timer = threading.Timer(0.35, self._save_settings)
        timer.daemon = True
        with self.lock:
            self._save_timer = timer
        timer.start()

    def _save_settings(self) -> None:
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(
                json.dumps(
                    {
                        "volume": self.volume,
                        "match_rate": self.match_rate,
                        "device_uid": self.device_uid,
                    },
                    indent=2,
                )
            )
        except OSError:
            pass

    def art(self, track_id: int) -> bytes | None:
        with self.lock:
            cached = self._art.get(track_id)
            track = next((item for item in self.queue if item.id == track_id), None)
        if cached is not None:
            return cached
        if track is None or not track.has_art:
            return None
        data = extract_art(track.path)
        if data:
            with self.lock:
                self._art[track_id] = data
        return data

    def add_paths(self, paths: list[str]) -> None:
        files = _collect(paths)
        if not files:
            with self.lock:
                self.error = "No audio files in that selection."
            return

        def work() -> None:
            found: list[Track] = []
            skipped = 0
            total = len(files)
            for offset, path in enumerate(files, start=1):
                with self.lock:
                    self.scanning = True
                    self.scan_label = f"Reading {offset} of {total}"
                    if any(item.path == path for item in self.queue) or any(
                        item.path == path for item in found
                    ):
                        continue
                    self._ids += 1
                    track_id = self._ids
                track = probe(path, track_id)
                if track is None:
                    skipped += 1
                    continue
                found.append(track)
            found.sort(
                key=lambda item: (
                    item.album.lower(),
                    item.disc_no,
                    item.track_no,
                    item.title.lower(),
                    item.path,
                )
            )
            with self.lock:
                self.queue.extend(found)
                if self.index < 0 and self.queue:
                    self.index = 0
                    self.position = 0.0
                self.scanning = False
                self.scan_label = ""
                if skipped and not found:
                    self.error = "Those files had no audio stream ffmpeg could read."
                elif found:
                    self.error = ""

        threading.Thread(target=work, daemon=True).start()

    def selected_device(self, devices: list[audiohw.Device] | None = None) -> audiohw.Device | None:
        devices = devices if devices is not None else audiohw.output_devices()
        if not devices:
            return None
        if self.device_uid:
            for device in devices:
                if device.uid == self.device_uid:
                    return device
        default_uid = audiohw.default_output_uid()
        for device in devices:
            if device.uid == default_uid:
                return device
        return devices[0]

    def plan(self, track: Track, device: audiohw.Device | None) -> Plan:
        if device is None:
            return Plan(0, "pcm_s16le", None, None, "NO DEVICE", "No output device.")
        resample_to = None
        clock = None
        if track.sample_rate and track.sample_rate in device.rates and self.match_rate:
            clock = float(track.sample_rate)
        elif track.sample_rate and track.sample_rate not in device.rates:
            resample_to = audiohw.nearest_rate(device.rates, track.sample_rate)

        filtered = resample_to is not None
        if filtered or track.grade in {"lossy", "dsd"} or track.bits <= 0:
            codec = "pcm_f32le"
            delivery = "32-BIT FLOAT"
        elif track.bits <= 16:
            codec = "pcm_s16le"
            delivery = "16-BIT INTEGER"
        else:
            codec = "pcm_s32le"
            delivery = f"{track.bits}-BIT IN 32"

        advice = self._advice(track, device, resample_to)
        return Plan(device.index, codec, resample_to, clock, delivery, advice)

    def _advice(
        self,
        track: Track,
        device: audiohw.Device,
        resample_to: int | None,
    ) -> str:
        file_rate = _khz(track.sample_rate) if track.sample_rate else "?"
        parts: list[str] = []
        if track.grade == "hires":
            if track.sample_rate in device.rates and self.match_rate:
                if abs(device.rate - track.sample_rate) < 1:
                    parts.append(f"Device clock is on {file_rate} kHz.")
                elif self.status == "playing":
                    parts.append("The device clock did not move, so CoreAudio may resample.")
                else:
                    parts.append(
                        f"Play sets {device.name} to {file_rate} kHz. The old clock returns on quit."
                    )
            elif resample_to is not None:
                parts.append(
                    f"{device.name} stops at {_khz(resample_to)} kHz, so this {file_rate} kHz file is resampled once with SoX."
                )
            else:
                parts.append(
                    f"The file is {file_rate} kHz. Match rate to put that clock on {device.name}."
                )
        elif track.grade == "dsd":
            if resample_to is not None:
                parts.append(f"DSD, decoded and brought to {_khz(resample_to)} kHz PCM.")
            else:
                parts.append("DSD, decoded to PCM.")
        elif track.grade == "lossless":
            parts.append(
                f"Lossless {track.bits or 16}-bit / {file_rate}. A 24-bit or 96 kHz master still has more air."
            )
        else:
            parts.append("Lossy source. Playback is clean. What it threw away stays gone.")
        return " ".join(parts)

    def snapshot(self) -> dict:
        devices = audiohw.output_devices()
        with self.lock:
            device = self.selected_device(devices)
            current = self.queue[self.index] if 0 <= self.index < len(self.queue) else None
            plan = self.plan(current, device) if current else None
            return {
                "ready": ffmpeg_ready() is None,
                "setup_error": ffmpeg_ready(),
                "status": self.status,
                "index": self.index,
                "position": self.position,
                "volume": self.volume,
                "match_rate": self.match_rate,
                "error": self.error,
                "notice": self.notice,
                "scanning": self.scanning,
                "scan_label": self.scan_label,
                "device_uid": device.uid if device else "",
                "devices": [
                    {
                        "index": item.index,
                        "uid": item.uid,
                        "name": item.name,
                        "rate": item.rate,
                        "rates": item.rates,
                    }
                    for item in devices
                ],
                "delivery": plan.delivery if plan else "",
                "advice": plan.advice if plan else "",
                "resample_to": plan.resample_to if plan else None,
                "queue": [item.public() for item in self.queue],
                "now": current.public() if current else None,
            }

    def set_device(self, uid: str) -> None:
        playing = False
        position = 0.0
        index = -1
        with self.lock:
            self.device_uid = uid
            self._save_settings()
            if self.status == "playing":
                playing = True
                position = self.position
                index = self.index
        if playing:
            self._restart(index, position)

    def set_match(self, enabled: bool) -> None:
        with self.lock:
            self.match_rate = bool(enabled)
            self._save_settings()
            playing = self.status == "playing"
            position = self.position
            index = self.index
        if not enabled:
            self._restore_rates()
        if playing:
            self._restart(index, position)

    def set_volume(self, volume: float) -> None:
        volume = min(1.0, max(0.0, float(volume)))
        with self.lock:
            if abs(volume - self.volume) < 0.0005:
                return
            self.volume = volume
            player = self._player
        if player is not None:
            player.set_volume(volume)
        self._save_settings_soon()

    def play_index(self, index: int) -> None:
        self._restart(index, 0.0)

    def toggle(self) -> None:
        with self.lock:
            status = self.status
            index = self.index
            position = self.position
            has_queue = bool(self.queue)
        if not has_queue:
            return
        if status == "playing":
            self.pause()
        elif status == "paused":
            self._restart(index if index >= 0 else 0, position)
        else:
            self._restart(index if index >= 0 else 0, 0.0)

    def pause(self) -> None:
        with self.lock:
            if self.status != "playing":
                return
            self.status = "paused"
            proc, player = self._take_proc()
        self._release(proc, player)

    def stop(self) -> None:
        with self.lock:
            self.status = "stopped"
            self.position = 0.0
            self._seek = 0.0
            proc, player = self._take_proc()
        self._release(proc, player)
        self._restore_rates()

    def seek(self, seconds: float) -> None:
        with self.lock:
            if not (0 <= self.index < len(self.queue)):
                return
            duration = self.queue[self.index].duration
            seconds = max(0.0, float(seconds))
            if duration:
                seconds = min(seconds, max(0.0, duration - 0.25))
            self.position = seconds
            status = self.status
            index = self.index
        if status == "playing":
            self._restart(index, seconds)
        else:
            with self.lock:
                self._seek = seconds
                if self.status == "stopped":
                    self.status = "paused"

    def next_track(self) -> None:
        with self.lock:
            index = self.index + 1 if self.index >= 0 else 0
        self._restart(index, 0.0)

    def prev_track(self) -> None:
        with self.lock:
            position = self.position
            index = self.index
            status = self.status
        if index < 0:
            return
        if position > 3 and status in {"playing", "paused"}:
            self.seek(0)
            return
        self._restart(max(0, index - 1), 0.0)

    def move(self, src: int, dst: int) -> None:
        with self.lock:
            count = len(self.queue)
            if not (0 <= src < count and 0 <= dst < count) or src == dst:
                return
            track = self.queue.pop(src)
            self.queue.insert(dst, track)
            current = self.index
            if current == src:
                self.index = dst
            elif src < current <= dst:
                self.index -= 1
            elif dst <= current < src:
                self.index += 1

    def remove(self, index: int) -> None:
        stopped = False
        proc = None
        player = None
        with self.lock:
            if not (0 <= index < len(self.queue)):
                return
            removing_current = self.index == index
            del self.queue[index]
            if removing_current:
                proc, player = self._take_proc()
                self.status = "stopped"
                self.position = 0.0
                self.index = min(index, len(self.queue) - 1) if self.queue else -1
                stopped = True
            elif self.index > index:
                self.index -= 1
        self._release(proc, player)
        if stopped:
            self._restore_rates()

    def clear(self) -> None:
        self.stop()
        with self.lock:
            self.queue.clear()
            self.index = -1
            self._art.clear()

    def shutdown(self) -> None:
        with self.lock:
            self.status = "stopped"
            proc, player = self._take_proc()
        self._release(proc, player)
        self._restore_rates()

    def _restart(self, index: int, position: float) -> None:
        missing = ffmpeg_ready()
        if missing:
            with self.lock:
                self.error = missing
            return
        device = self.selected_device()
        with self.lock:
            if not (0 <= index < len(self.queue)):
                self.status = "stopped"
                self.position = 0.0
                proc, player = self._take_proc()
                restore = True
                track = None
                plan = None
                gen = self._gen
            else:
                restore = False
                track = self.queue[index]
                plan = self.plan(track, device)
                self._gen += 1
                gen = self._gen
                self.index = index
                self._seek = position
                self.position = position
                self.status = "playing"
                self.error = ""
                self.notice = ""
                proc, player = self._take_proc()
        self._release(proc, player)
        if restore or track is None or plan is None:
            self._restore_rates()
            return
        if device is None:
            with self.lock:
                self.status = "stopped"
                self.error = "No output device."
            return
        try:
            self._apply_clock(device, plan.clock)
        except OSError as exc:
            with self.lock:
                self.notice = str(exc)
        self._spawn(track, position, plan, gen, device.uid, fold=False)

    def _apply_clock(self, device: audiohw.Device, target: float | None) -> None:
        if target is None:
            return
        current = audiohw.read_rate(device.audio_id)
        if abs(current - target) < 1:
            return
        with self.lock:
            self._saved_rates.setdefault(device.audio_id, current)
        audiohw.set_rate(device.audio_id, target)
        deadline = time.time() + 0.45
        while time.time() < deadline:
            if abs(audiohw.read_rate(device.audio_id) - target) < 1:
                return
            time.sleep(0.04)

    def _restore_rates(self) -> None:
        with self.lock:
            saved = self._saved_rates
            self._saved_rates = {}
        for audio_id, rate in saved.items():
            try:
                audiohw.set_rate(audio_id, rate)
            except OSError:
                pass

    def _spawn(
        self,
        track: Track,
        start: float,
        plan: Plan,
        gen: int,
        device_uid: str,
        fold: bool,
    ) -> None:
        with self.lock:
            level = self.volume
        raw = {"pcm_s16le": "s16le", "pcm_s32le": "s32le", "pcm_f32le": "f32le"}.get(plan.codec)
        if raw is None:
            with self.lock:
                if gen == self._gen:
                    self.status = "stopped"
                    self.error = "This file has no playable format."
            return
        rate = int(plan.resample_to or track.sample_rate or 44100)
        channels = 2 if fold else (track.channels or 2)
        filters: list[str] = []
        if plan.resample_to:
            filters.append(f"aresample=resampler=soxr:precision=33:osr={rate}")
        command = [
            FFMPEG,
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, start):.3f}",
            "-i",
            track.path,
            "-vn",
            "-sn",
            "-dn",
        ]
        if filters:
            command += ["-af", ",".join(filters)]
        if fold:
            command += ["-ac", "2"]
        command += ["-f", raw, "pipe:1"]
        try:
            player = output.open_output(rate, channels, plan.codec, device_uid, level)
        except OSError as exc:
            with self.lock:
                if gen == self._gen:
                    self.status = "stopped"
                    self.error = str(exc)
            return
        with self.lock:
            if gen != self._gen or self.status != "playing":
                player.close()
                return
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            player.close()
            with self.lock:
                if gen == self._gen:
                    self.status = "stopped"
                    self.error = str(exc)
            return
        with self.lock:
            if gen != self._gen or self.status != "playing":
                player.close()
                proc.terminate()
                return
            self._proc = proc
            self._player = player
            pending = self.volume
        if abs(pending - level) >= 0.0005:
            player.set_volume(pending)
        threading.Thread(
            target=self._watch,
            args=(proc, player, gen, track, plan, start, device_uid, fold),
            daemon=True,
        ).start()

    def _still(self, gen: int) -> bool:
        with self.lock:
            return gen == self._gen and self.status == "playing"

    def _note_position(self, gen: int, start: float, player: output.MacOutput) -> None:
        heard = start + player.seconds()
        with self.lock:
            if gen == self._gen and self.status == "playing":
                self.position = heard

    def _watch(
        self,
        proc: subprocess.Popen,
        player: output.MacOutput,
        gen: int,
        track: Track,
        plan: Plan,
        start: float,
        device_uid: str,
        fold: bool,
    ) -> None:
        errors: list[bytes] = []

        def drain() -> None:
            if proc.stderr is not None:
                errors.append(proc.stderr.read())

        drainer = threading.Thread(target=drain, daemon=True)
        drainer.start()
        failure = ""
        got_audio = False
        try:
            if proc.stdout is not None:
                while self._still(gen):
                    chunk = proc.stdout.read(32768)
                    if not chunk:
                        break
                    player.write(chunk)
                    got_audio = True
                    self._note_position(gen, start, player)
            if self._still(gen):
                player.finish()
                while self._still(gen) and not player.drained():
                    self._note_position(gen, start, player)
                    time.sleep(0.03)
        except OSError as exc:
            failure = str(exc)
        code = proc.wait()
        drainer.join(timeout=1)
        message = failure or b"".join(errors).decode(errors="replace").strip()
        action = "ignore"
        proc_ref = None
        player_ref = None
        with self.lock:
            playing = gen == self._gen and self.status == "playing"
            finished = playing and got_audio and player.drained() and not failure
            if not playing:
                action = "ignore"
            elif message and not finished:
                lower = message.lower()
                channel_problem = any(
                    word in lower for word in ("channel", "format", "rejected", "unsupported")
                )
                if not fold and track.channels > 2 and (channel_problem or bool(failure)):
                    self.notice = "Folded to stereo for this device."
                    action = "fold"
                else:
                    self.status = "stopped"
                    self.error = message.splitlines()[-1][:400]
                    proc_ref, player_ref = self._take_proc()
                    action = "error"
            elif finished or (playing and got_audio and code == 0):
                action = "advance"
        if action == "error":
            self._release(proc_ref, player_ref)
            self._restore_rates()
        elif action == "fold":
            with self.lock:
                old_proc, old_player = self._take_proc()
            self._release(old_proc, old_player)
            self._spawn(track, start, plan, gen, device_uid, fold=True)
        elif action == "advance":
            with self.lock:
                try:
                    current = next(i for i, item in enumerate(self.queue) if item.id == track.id)
                except StopIteration:
                    current = len(self.queue) - 1
            self._restart(current + 1, 0.0)

    def _take_proc(self) -> tuple[subprocess.Popen | None, output.MacOutput | None]:
        proc = self._proc
        player = self._player
        self._proc = None
        self._player = None
        return proc, player

    def _release(self, proc: subprocess.Popen | None, player: output.MacOutput | None = None) -> None:
        if player is not None:
            player.close()
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=0.4)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                pass

