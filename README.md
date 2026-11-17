# LOSSLOSS

A small hi-res audio player. ffmpeg decodes the file. The output queue plays it. The window is just a desert-rock face on that path.

Not affiliated with Queens of the Stone Age.

## Run

From a terminal, `./lossloss` uses python3, ffmpeg, and ffprobe already on the Mac. They are picked up from `/usr/local/bin` or `/opt/homebrew/bin` as well as `PATH`.

Open **LOSSLOSS** from Applications. Quit from the window and the output device's previous sample rate is put back.

Build a disk image to hand to someone else with:

```sh
mac/package.sh
```

That writes `mac/dist/LOSSLOSS.dmg` and replaces `~/Applications/LOSSLOSS.app`. The app in the image carries Python, ffmpeg, and ffprobe, for both Intel and Apple Silicon. Open the image and drag LOSSLOSS onto Applications.

The first time that Mac opens it, macOS says the developer cannot be verified. Right-click the app, choose Open, and confirm. Removing that prompt takes an Apple Developer ID and notarization, which this build does not have.

From the project, the same player can be started in a terminal:

```sh
./lossloss
./lossloss ~/Music/album
./lossloss --browser
```

## What “hi-res” means here

A file is marked **hi-res** when it is lossless and either 24-bit or greater, or 88.2 kHz or greater. CD-quality FLAC stays **lossless**. MP3, AAC, and the rest stay **lossy**.

At full level, 16-bit audio is sent as 16-bit integer and 24-bit audio is sent as 32-bit integer frames with the original 24 bits intact. ffmpeg is not asked to resample. The level knob sets the output gain, so audio already queued changes with the knob. **Match rate** sets the chosen output device to the file's sample rate when that device advertises the rate, so CoreAudio is not asked to convert the clock. The previous clock is restored when you quit, stop, or turn match off.

If the device cannot clock that high, the file is resampled once with SoX to the nearest lower rate the device does advertise. The level knob does not restart the song. Integer files stay on integer frames until the output applies the gain.

This is clock matching, not exclusive hog mode. macOS can still mix other apps into the device.

Built-in speakers on a recent MacBook Pro advertise up to 96 kHz. A virtual device such as BlackHole advertises much higher clocks, which is the practical way to hand 176.4 and 192 kHz to a DAC.
