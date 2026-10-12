# Live Voice Translate — English to Ukrainian

A modified version of [live-voice-translate](https://github.com/stephdl/live-voice-translate).

This version is being developed for local, real-time English speech recognition and Ukrainian translation, with subtitles displayed in a transparent desktop overlay.

## Features

- English speech recognition using faster-whisper.
- English-to-Ukrainian translation using Argos Translate.
- Transparent GTK subtitle overlay.
- Captures system audio through PulseAudio/PipeWire monitor sources.
- Voice activity detection using WebRTC VAD.
- CPU inference by default; optional CUDA mode.
- Optional transcript logging to a text file.
- Adjustable overlay: drag to move, use the bottom-right corner to resize, double-click to switch display size, and use the mouse wheel to browse subtitle history.

This project is a work in progress. It has been tested on Linux Mint 22.3 with Python 3.12. Other distributions and desktop environments have not yet been verified.

## How it works

1. Captures system playback audio through a PulseAudio/PipeWire monitor source.
2. Detects speech and separates audio into chunks.
3. Transcribes English speech locally with faster-whisper.
4. Translates recognized text into Ukrainian with Argos Translate.
5. Displays the translated subtitles in the overlay.

The Whisper model and Argos language package may need to be downloaded before first use. Audio transcription and translation are processed locally after the required models and packages are installed. This application does not guarantee that every recognized phrase or translation will be correct.

## Requirements

- Linux with a working PulseAudio or PipeWire audio server.
- Python 3.12 recommended (the tested environment).
- GTK 3, PyGObject, and Cairo.
- `pactl` for audio-source discovery.
- Python packages: `faster-whisper`, `argostranslate`, and `webrtcvad`.

## Installation

### 1. Install system dependencies

On Linux Mint or Ubuntu:

```bash
sudo apt update
sudo apt install python3-venv python3-dev python3-gi gir1.2-gtk-3.0 python3-cairo pulseaudio-utils
```

### 2. Create the Python environment

From the directory where you want to keep the project:

```bash
python3 -m venv --system-site-packages ~/.local/share/live-voice-translate/venv
~/.local/share/live-voice-translate/venv/bin/python -m pip install --upgrade pip
~/.local/share/live-voice-translate/venv/bin/python -m pip install -r requirements.txt
```

If the virtual environment already exists, you can reuse it rather than creating it again.

### 3. Install the English-to-Ukrainian Argos language package

Run this once while connected to the internet:

```bash
~/.local/share/live-voice-translate/venv/bin/python - <<'PY'
import argostranslate.package

packages = argostranslate.package.get_available_packages()
package = next(
    (p for p in packages if p.from_code == "en" and p.to_code == "uk"),
    None,
)

if package is None:
    raise SystemExit("English-to-Ukrainian Argos package was not found.")

path = package.download()
argostranslate.package.install_from_path(path)
print("English-to-Ukrainian package installed.")
PY
```

The application currently expects the `en` to `uk` translation package to be installed. It does not automatically install that package.

### 4. Start the application

Run from the project directory:

```bash
PYTHONPATH="/usr/lib/python3/dist-packages${PYTHONPATH:+:$PYTHONPATH}" \
  ~/.local/share/live-voice-translate/venv/bin/python lvt.py --model tiny --to uk
```

The first launch may take longer because faster-whisper needs to download the selected model.

## Command-line options

```text
--model   tiny | base | small | medium | large-v3
--to      uk
--mode    fast | normal | slow
--save    PATH
--gpu     Try CUDA inference, falling back to CPU if initialization fails
```

Examples:

```bash
# Default lightweight model
python lvt.py --model tiny --to uk

# Try a larger model for potentially better recognition
python lvt.py --model small --to uk

# Save recognized English and Ukrainian text with timestamps and latency
python lvt.py --model tiny --to uk --save session.txt

# Try CUDA if supported by the installed software and hardware
python lvt.py --model small --to uk --gpu
```

The available processing modes are exposed by the command-line interface, but their behavior may change while the application is being developed.

## Audio troubleshooting

List available audio sources:

```bash
pactl list short sources
```

Look for a source ending in `.monitor`. Play audio through the device you want to capture, then restart the application if the wrong source is selected.

If no monitor source is available, verify that PipeWire/PulseAudio is running and that system playback works.

## Known limitations

- Recognition quality depends on the audio quality, accent, background noise, and selected Whisper model.
- Translation quality varies; proper names, idioms, and unusual phrasing may be mistranslated.
- Larger Whisper models usually require more memory and processing time.
- CUDA mode requires a compatible setup; CPU mode is the default.
- Tested primarily on Linux Mint 22.3. Compatibility with other distributions is not guaranteed.

## Feedback and contributions

Feedback, bug reports, and pull requests are welcome. Please include your Linux distribution, Python version, selected Whisper model, and relevant error messages when reporting a problem. Do not include private conversations or sensitive recordings in public reports.

This is an independent modification of the upstream project. It is not an official release of the original repository.

## Credits and license

The original project is by [Stéphane de Labrusse](https://github.com/stephdl). This version builds on that work and uses:

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [Argos Translate](https://github.com/argosopentech/argos-translate)
- [py-webrtcvad](https://github.com/wiseman/py-webrtcvad)
- GTK 3 and PyGObject

The upstream project includes the GNU General Public License, version 3 or later. The accompanying `LICENSE` file is retained from the upstream repository. Review it before redistributing modified copies.
