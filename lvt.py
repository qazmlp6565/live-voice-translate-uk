#!/usr/bin/env python3
"""
Modified version of live-voice-translate.
Original project: https://github.com/stephdl/live-voice-translate

Original copyright (C) 2026 Stéphane de Labrusse.

This modified version is distributed under the GNU General Public
License, version 3 or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

See the accompanying LICENSE file.
"""


import argparse
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import wave

# GTK/PyGObject is installed by the system Python on Ubuntu/Mint.
# Make it visible from the project virtual environment.
_SYSTEM_DIST_PACKAGES = "/usr/lib/python3/dist-packages"
if os.path.isdir(_SYSTEM_DIST_PACKAGES):
    sys.path.insert(0, _SYSTEM_DIST_PACKAGES)
from collections import deque
from datetime import datetime

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango, PangoCairo
import cairo

from faster_whisper import WhisperModel
import argostranslate.translate
import webrtcvad


# ============================================================
# Configuration
# ============================================================

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2

VAD_FRAME_MS = 20
VAD_FRAME_BYTES = int(SAMPLE_RATE * VAD_FRAME_MS / 1000) * SAMPLE_WIDTH

PRE_ROLL_MS = 300
END_SILENCE_MS = 700
MAX_UTTERANCE_MS = 9000
MIN_SPEECH_MS = 250

SENTENCE_WAIT_SECONDS = 1.25
MIN_DISPLAY_SECONDS = 2.2
MAX_DISPLAY_SECONDS = 6.0

HISTORY_LIMIT = 100


# ============================================================
# Helpers
# ============================================================

def now_string():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def clean_text(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def ends_sentence(text):
    text = text.rstrip()
    return bool(re.search(r"[.!?…][\"'»”)]*$", text))


def get_active_monitor():
    """
    Find a PipeWire/PulseAudio monitor source.
    Prefer one currently RUNNING.
    """
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None

    running = []
    monitors = []

    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue

        name = parts[1]
        state = parts[-1]

        if ".monitor" in name:
            monitors.append(name)
            if state.upper() == "RUNNING":
                running.append(name)

    if running:
        return running[0]

    if monitors:
        return monitors[0]

    return None


# ============================================================
# GTK/Cairo subtitle overlay
# ============================================================

class SubtitleWindow:
    def __init__(self):
        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)

        self.window.set_title("Live Voice Translate")
        self.window.set_decorated(False)
        self.window.set_resizable(True)
        self.window.set_keep_above(True)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)

        # RGBA visual: transparent outside our drawn rectangle.
        screen = self.window.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.window.set_visual(visual)

        self.window.set_app_paintable(True)

        self.compact_width = 900
        self.compact_height = 105

        self.expanded_width = 1100
        self.expanded_height = 180

        self.window.set_default_size(
            self.compact_width,
            self.compact_height,
        )

        self.compact = True

        self.text = ""
        self.status = ""

        self.history = []
        self.history_index = None

        self.dragging = False
        self.resizing = False
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self.resize_start_x = 0
        self.resize_start_y = 0
        self.resize_start_w = 0
        self.resize_start_h = 0

        self.last_display_time = 0.0

        self.window.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.SMOOTH_SCROLL_MASK
        )

        self.window.connect("draw", self._on_draw)
        self.window.connect("button-press-event", self._on_button_press)
        self.window.connect("button-release-event", self._on_button_release)
        self.window.connect("motion-notify-event", self._on_motion)
        self.window.connect("scroll-event", self._on_scroll)
        self.window.connect("destroy", self._on_destroy)

        self.window.show_all()

    # --------------------------------------------------------
    # Drawing
    # --------------------------------------------------------

    def _on_draw(self, widget, cr):
        allocation = widget.get_allocation()
        width = allocation.width
        height = allocation.height

        # Completely transparent background.
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()

        # Subtitle background.
        margin = 5
        radius = 14

        x = margin
        y = margin
        w = max(20, width - margin * 2)
        h = max(20, height - margin * 2)

        cr.set_operator(cairo.OPERATOR_OVER)

        cr.new_sub_path()
        cr.arc(x + w - radius, y + radius, radius, -1.5708, 0)
        cr.arc(x + w - radius, y + h - radius, radius, 0, 1.5708)
        cr.arc(x + radius, y + h - radius, radius, 1.5708, 3.14159)
        cr.arc(x + radius, y + radius, radius, 3.14159, 4.71239)
        cr.close_path()

        # Semi-transparent black background.
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.72)
        cr.fill()

        if self.text:
            layout = PangoCairo.create_layout(cr)

            font_size = 25 if self.compact else 28
            font = Pango.FontDescription()
            font.set_family("DejaVu Sans")
            font.set_weight(Pango.Weight.BOLD)
            font.set_absolute_size(font_size * Pango.SCALE)

            layout.set_font_description(font)

            text_width = max(100, width - 55)
            text_height = max(50, height - 30)

            layout.set_width(text_width * Pango.SCALE)
            layout.set_height(text_height * Pango.SCALE)
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
            layout.set_ellipsize(Pango.EllipsizeMode.NONE)
            layout.set_alignment(Pango.Alignment.CENTER)
            layout.set_text(self.text, -1)

            ink, logical = layout.get_pixel_extents()

            text_y = max(
                10,
                int((height - logical.height) / 2),
            )

            cr.save()

            # Fully opaque white text.
            cr.set_source_rgba(1.0, 1.0, 1.0, 1.0)

            PangoCairo.update_layout(cr, layout)
            # Center the entire layout; Pango centers text inside it.
            cr.move_to(
                (width - text_width) / 2,
                text_y,
            )
            PangoCairo.show_layout(cr, layout)

            cr.restore()

        # Tiny resize grip in bottom-right corner.
        # It is intentionally subtle.
        if self.compact:
            grip = 13
        else:
            grip = 17

        cr.set_line_width(1.5)
        cr.set_source_rgba(1, 1, 1, 0.35)

        for i in range(3):
            offset = i * 4
            cr.move_to(
                width - 7 - offset,
                height - 7,
            )
            cr.line_to(
                width - 7,
                height - 7 - offset,
            )

        cr.stroke()

        return False

    # --------------------------------------------------------
    # Mouse handling
    # --------------------------------------------------------

    def _in_resize_corner(self, x, y):
        allocation = self.window.get_allocation()
        return (
            x >= allocation.width - 28
            and y >= allocation.height - 28
        )

    def _on_button_press(self, widget, event):
        if event.button == 1:
            if self._in_resize_corner(event.x, event.y):
                self.resizing = True
                self.dragging = False

                self.resize_start_x = event.x_root
                self.resize_start_y = event.y_root

                allocation = self.window.get_allocation()
                self.resize_start_w = allocation.width
                self.resize_start_h = allocation.height

                return True

            # Double-click toggles compact/expanded.
            if event.type == Gdk.EventType._2BUTTON_PRESS:
                self.toggle_size()
                return True

            self.dragging = True

            allocation = self.window.get_position()
            self.drag_offset_x = int(event.x_root - allocation.root_x)
            self.drag_offset_y = int(event.y_root - allocation.root_y)

            return True

        return False

    def _on_button_release(self, widget, event):
        if event.button == 1:
            self.dragging = False
            self.resizing = False
            return True

        return False

    def _on_motion(self, widget, event):
        if self.dragging:
            new_x = int(event.x_root - self.drag_offset_x)
            new_y = int(event.y_root - self.drag_offset_y)

            self.window.move(new_x, new_y)
            return True

        if self.resizing:
            dx = int(event.x_root - self.resize_start_x)
            dy = int(event.y_root - self.resize_start_y)

            new_w = max(500, self.resize_start_w + dx)
            new_h = max(80, self.resize_start_h + dy)

            self.window.resize(new_w, new_h)
            self.compact = new_h < 140

            self.window.queue_draw()
            return True

        return False

    def _on_scroll(self, widget, event):
        direction = event.direction

        if direction == Gdk.ScrollDirection.UP:
            self.show_previous()

        elif direction == Gdk.ScrollDirection.DOWN:
            self.show_next()

        elif direction == Gdk.ScrollDirection.SMOOTH:
            if event.delta_y < 0:
                self.show_previous()
            elif event.delta_y > 0:
                self.show_next()

        return True

    # --------------------------------------------------------
    # History
    # --------------------------------------------------------

    def add_history(self, text):
        text = clean_text(text)

        if not text:
            return

        self.history.append(text)

        if len(self.history) > HISTORY_LIMIT:
            self.history = self.history[-HISTORY_LIMIT:]

        self.history_index = None

    def show_previous(self):
        if not self.history:
            return

        if self.history_index is None:
            self.history_index = len(self.history) - 1
        elif self.history_index > 0:
            self.history_index -= 1

        self.text = self.history[self.history_index]
        self.status = "history"
        self.window.queue_draw()

    def show_next(self):
        if not self.history:
            return

        if self.history_index is None:
            return

        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            self.text = self.history[self.history_index]
        else:
            self.history_index = None
            if self.history:
                self.text = self.history[-1]

        self.status = "history"
        self.window.queue_draw()

    # --------------------------------------------------------
    # Public UI methods
    # --------------------------------------------------------

    def display(self, text):
        text = clean_text(text)
        if not text:
            return
        self.add_history(text)
        self.text = text
        self.status = "live"
        self.last_display_time = time.monotonic()
        self.window.queue_draw()

    def display_preview(self, text):
        text = clean_text(text)
        if not text:
            return
        self.text = text
        self.status = "live"
        self.window.queue_draw()

    def toggle_size(self):
        self.compact = not self.compact

        if self.compact:
            self.window.resize(
                self.compact_width,
                self.compact_height,
            )
        else:
            self.window.resize(
                self.expanded_width,
                self.expanded_height,
            )

        self.window.queue_draw()

    def _on_destroy(self, *_):
        Gtk.main_quit()

    def close(self):
        try:
            self.window.destroy()
        except Exception:
            pass


# ============================================================
# Audio capture + VAD
# ============================================================

class AudioCapture:
    def __init__(self, output_queue, stop_event):
        self.output_queue = output_queue
        self.stop_event = stop_event

        self.process = None
        self.thread = None

        self.vad = webrtcvad.Vad(2)

        self.pre_roll_frames = max(
            1,
            PRE_ROLL_MS // VAD_FRAME_MS,
        )

        self.end_silence_frames = max(
            1,
            END_SILENCE_MS // VAD_FRAME_MS,
        )

        self.max_frames = max(
            1,
            MAX_UTTERANCE_MS // VAD_FRAME_MS,
        )

        self.min_speech_frames = max(
            1,
            MIN_SPEECH_MS // VAD_FRAME_MS,
        )

        self.started_at = None

    def start(self):
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
        )
        self.thread.start()

    def _run(self):
        print("🎧 Waiting for audio stream...", flush=True)
        print(
            "   Start Discord/video/audio playback when ready.",
            flush=True,
        )

        while not self.stop_event.is_set():
            source = get_active_monitor()

            if source:
                print(
                    f"🎧 Audio source: {source}",
                    flush=True,
                )

                if self._capture_source(source):
                    break

            self.stop_event.wait(0.5)

    def _capture_source(self, source):
        try:
            self.process = subprocess.Popen(
                [
                    "parec",
                    "--device", source,
                    "--format=s16le",
                    "--rate=16000",
                    "--channels=1",
                    "--raw",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except Exception as e:
            print(
                f"❌ Cannot start parec: {e}",
                flush=True,
            )
            return False

        frame_buffer = b""

        pre_roll = deque(
            maxlen=self.pre_roll_frames
        )

        speech_frames = []
        in_speech = False
        silence_count = 0
        total_speech_frames = 0
        utterance_start = None
        last_partial_ms = 0

        try:
            while not self.stop_event.is_set():
                data = self.process.stdout.read(
                    VAD_FRAME_BYTES
                )

                if not data:
                    break

                frame_buffer += data

                while len(frame_buffer) >= VAD_FRAME_BYTES:
                    frame = frame_buffer[:VAD_FRAME_BYTES]
                    frame_buffer = frame_buffer[VAD_FRAME_BYTES:]

                    try:
                        is_speech = self.vad.is_speech(
                            frame,
                            SAMPLE_RATE,
                        )
                    except Exception:
                        is_speech = False

                    pre_roll.append(frame)

                    if not in_speech:
                        if is_speech:
                            in_speech = True
                            silence_count = 0
                            total_speech_frames = 0

                            utterance_start = (
                                time.monotonic()
                                - (len(pre_roll) * VAD_FRAME_MS / 1000.0)
                            )

                            speech_frames = list(pre_roll)
                            total_speech_frames = len(speech_frames)
                            last_partial_ms = 0
                        continue

                    speech_frames.append(frame)

                    if is_speech:
                        silence_count = 0
                        total_speech_frames += 1
                    else:
                        silence_count += 1

                    elapsed_ms = (
                        len(speech_frames)
                        * VAD_FRAME_MS
                    )

                    reached_silence = (
                        silence_count
                        >= self.end_silence_frames
                    )

                    reached_max = (
                        elapsed_ms
                        >= MAX_UTTERANCE_MS
                    )

                    if reached_silence or reached_max:
                        if (
                            total_speech_frames
                            >= self.min_speech_frames
                        ):
                            audio = b"".join(
                                speech_frames
                            )

                            self.output_queue.put(
                                {
                                    "audio": audio,
                                    "speech_start": utterance_start,
                                    "speech_end": time.monotonic(),
                                    "partial": False,
                                }
                            )

                        in_speech = False
                        silence_count = 0
                        total_speech_frames = 0
                        speech_frames = []
                        pre_roll.clear()
                        utterance_start = None

        finally:
            self._stop_process()

        return True

    def _stop_process(self):
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=1)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass

            self.process = None

    def stop(self):
        self._stop_process()


# ============================================================
# Main translator
# ============================================================

class LiveTranslator:
    def __init__(
        self,
        model_name="tiny",
        target_lang="uk",
        mode="normal",
        save_file=None,
        use_gpu=False,
    ):
        self.model_name = model_name
        self.target_lang = target_lang
        self.mode = mode
        self.save_file = save_file
        self.use_gpu = use_gpu

        self.stop_event = threading.Event()

        self.audio_queue = queue.Queue()
        self.translation_queue = queue.Queue()

        self.audio_capture = None

        self.model = None

        self.subtitle_window = SubtitleWindow()

        self.worker_thread = None

        self.pending_en = ""
        self.pending_start = None
        self.pending_last_end = None

        self.stats_count = 0

        self.transcript_lock = threading.Lock()

        self._install_translation_model()

    # --------------------------------------------------------
    # Translation
    # --------------------------------------------------------

    def _install_translation_model(self):
        try:
            languages = (
                argostranslate.translate
                .get_installed_languages()
            )

            en = next(
                (
                    lang
                    for lang in languages
                    if lang.code == "en"
                ),
                None,
            )

            uk = next(
                (
                    lang
                    for lang in languages
                    if lang.code == "uk"
                ),
                None,
            )

            if en is None or uk is None:
                print(
                    "⚠️ Argos EN→UK package is not installed.",
                    flush=True,
                )
                return

            installed = False

            for package in en.translations_from:
                if package.to_lang.code == "uk":
                    installed = True
                    break

            if not installed:
                print(
                    "⚠️ Argos EN→UK translation pair not found.",
                    flush=True,
                )

        except Exception as e:
            print(
                f"⚠️ Argos initialization warning: {e}",
                flush=True,
            )

    def translate(self, text):
        text = clean_text(text)

        if not text:
            return ""

        try:
            return clean_text(
                argostranslate.translate.translate(
                    text,
                    "en",
                    self.target_lang,
                )
            )
        except Exception as e:
            print(
                f"❌ Translation error: {e}",
                flush=True,
            )
            return ""

    # --------------------------------------------------------
    # Whisper
    # --------------------------------------------------------

    def setup_model(self):
        print(
            f"⏳ Loading Whisper model {self.model_name}...",
            flush=True,
        )

        if self.use_gpu:
            device = "cuda"
            compute_type = "float16"
        else:
            device = "cpu"
            compute_type = "int8"

        try:
            self.model = WhisperModel(
                self.model_name,
                device=device,
                compute_type=compute_type,
            )
        except Exception as e:
            if self.use_gpu:
                print(
                    f"⚠️ GPU initialization failed: {e}",
                    flush=True,
                )
                print(
                    "   Falling back to CPU...",
                    flush=True,
                )

                self.model = WhisperModel(
                    self.model_name,
                    device="cpu",
                    compute_type="int8",
                )
            else:
                raise

        print("✅ Whisper ready.", flush=True)
        print("", flush=True)

    def transcribe(self, audio, condition_on_previous_text=True):
        whisper_started = time.monotonic()
        audio_seconds = len(audio) / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH)
        temp_name = None

        try:
            temp_name = (
                f"/tmp/lvt_{os.getpid()}_"
                f"{threading.get_ident()}.wav"
            )

            with wave.open(temp_name, "wb") as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(SAMPLE_WIDTH)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(audio)

            segments, _ = self.model.transcribe(
                temp_name,
                language="en",
                task="transcribe",
                beam_size=1,
                best_of=1,
                temperature=0.0,
                condition_on_previous_text=condition_on_previous_text,
                vad_filter=False,
                word_timestamps=False,
            )

            parts = []

            for segment in segments:
                text = clean_text(segment.text)

                if text:
                    parts.append(text)

            result = clean_text(" ".join(parts))
            whisper_elapsed = time.monotonic() - whisper_started
            print(
                f"  WHISPER: {whisper_elapsed * 1000:.0f} ms "
                f"| audio {audio_seconds:.2f} s "
                f"| real-time x{whisper_elapsed / audio_seconds:.2f}"
                if audio_seconds > 0
                else f"  WHISPER: {whisper_elapsed * 1000:.0f} ms",
                flush=True,
            )
            return result

        except Exception as e:
            print(
                f"❌ Whisper error: {e}",
                flush=True,
            )
            return ""

        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except Exception:
                    pass

    # --------------------------------------------------------
    # Sentence buffering
    # --------------------------------------------------------

    def add_transcription(
        self,
        text,
        speech_start,
        speech_end,
    ):
        text = clean_text(text)

        if not text:
            return

        if not self.pending_en:
            self.pending_start = speech_start

        if self.pending_en:
            self.pending_en += " " + text
        else:
            self.pending_en = text

        self.pending_en = clean_text(self.pending_en)
        self.pending_last_end = speech_end

        # Commit naturally completed sentences immediately.
        if ends_sentence(self.pending_en):
            self.commit_pending("punctuation")
            return

        # Avoid huge subtitle blocks.
        if len(self.pending_en) >= 140:
            self.commit_pending("length")
            return

        # Also avoid accumulating indefinitely.
        if (
            self.pending_start is not None
            and time.monotonic() - self.pending_start > 7.0
        ):
            self.commit_pending("timeout")

    def maybe_commit_pending(self):
        if not self.pending_en:
            return

        if self.pending_last_end is None:
            return

        idle = time.monotonic() - self.pending_last_end

        if idle >= SENTENCE_WAIT_SECONDS:
            self.commit_pending("silence")

    def commit_pending(self, reason):
        text_en = clean_text(self.pending_en)

        if not text_en:
            self.pending_en = ""
            self.pending_start = None
            self.pending_last_end = None
            return

        speech_start = self.pending_start

        self.pending_en = ""
        self.pending_start = None
        self.pending_last_end = None

        translation_started = time.monotonic()

        text_uk = self.translate(text_en)

        translation_ready = time.monotonic()
        translation_latency = translation_ready - translation_started

        if not text_uk:
            return

        self.stats_count += 1

        total_latency = (
            translation_ready - speech_start
        )

        print(
            f"\n[{now_string()}] "
            f"LATENCY {total_latency * 1000:.0f} ms "
            f"(translation "
            f"{translation_latency * 1000:.0f} ms)",
            flush=True,
        )

        print(
            f"  EN: {text_en}",
            flush=True,
        )

        print(
            f"  UK: {text_uk}",
            flush=True,
        )

        print(
            f"  Reason: {reason}",
            flush=True,
        )

        self._save_transcript(
            text_en,
            text_uk,
            total_latency,
        )

        GLib.idle_add(
            self._display_with_hold,
            text_uk,
        )

    # --------------------------------------------------------
    # Display timing
    # --------------------------------------------------------

    def _display_with_hold(self, text):
        """
        Keep a subtitle readable for a minimum time.
        If a new subtitle arrives too quickly, delay it.
        """

        elapsed = (
            time.monotonic()
            - self.subtitle_window.last_display_time
        )

        delay = max(
            0.0,
            MIN_DISPLAY_SECONDS - elapsed,
        )

        if delay <= 0:
            self.subtitle_window.display(text)
            return False

        GLib.timeout_add(
            int(delay * 1000),
            self._delayed_display,
            text,
        )

        return False

    def _delayed_display(self, text):
        self.subtitle_window.display(text)
        return False

    # --------------------------------------------------------
    # Processing worker
    # --------------------------------------------------------

    def processing_loop(self):
        while not self.stop_event.is_set():
            try:
                item = self.audio_queue.get(timeout=0.2)

                audio = item["audio"]
                speech_start = item["speech_start"]
                speech_end = item["speech_end"]

                # Ignore interim audio items; only final utterances are processed.
                if item.get("partial", False):
                    continue

                text = self.transcribe(audio)
                if text:
                    self.add_transcription(
                        text,
                        speech_start,
                        speech_end,
                    )

            except queue.Empty:
                self.maybe_commit_pending()

        # Flush anything left.
        if self.pending_en:
            self.commit_pending("shutdown")

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    def _save_transcript(
        self,
        text_en,
        text_uk,
        latency,
    ):
        if not self.save_file:
            return

        try:
            with self.transcript_lock:
                with open(
                    self.save_file,
                    "a",
                    encoding="utf-8",
                ) as f:
                    f.write(
                        f"[{datetime.now().isoformat(timespec='seconds')}] "
                        f"{latency * 1000:.0f} ms\n"
                    )
                    f.write(f"EN: {text_en}\n")
                    f.write(f"UK: {text_uk}\n\n")

        except Exception as e:
            print(
                f"⚠️ Cannot save transcript: {e}",
                flush=True,
            )

    # --------------------------------------------------------
    # Run
    # --------------------------------------------------------

    def start(self):
        self.setup_model()

        self.audio_capture = AudioCapture(
            self.audio_queue,
            self.stop_event,
        )

        self.audio_capture.start()

        self.worker_thread = threading.Thread(
            target=self.processing_loop,
            daemon=True,
        )
        self.worker_thread.start()

        print("──────────────────────────────────────────────────")
        print("Live Voice Translate")
        print("──────────────────────────────────────────────────")
        print(f"Whisper : {self.model_name}")
        print("Input   : system audio / PipeWire monitor")
        print("From    : English")
        print("To      : Ukrainian")
        print("VAD     : WebRTC")
        print("")
        print("Overlay controls:")
        print("  Left drag       → move")
        print("  Bottom-right    → resize")
        print("  Double click    → compact / expanded")
        print("  Mouse wheel ↑   → previous subtitle")
        print("  Mouse wheel ↓   → next subtitle")
        print("")
        print("Ctrl+C → stop")
        print("──────────────────────────────────────────────────")
        print("", flush=True)

        signal.signal(
            signal.SIGINT,
            self._signal_stop,
        )

        try:
            Gtk.main()
        except KeyboardInterrupt:
            self.stop()

    def _signal_stop(self, *_):
        GLib.idle_add(self.stop)

    def stop(self):
        if self.stop_event.is_set():
            return False

        print(
            "\n\nStopping translation...",
            flush=True,
        )

        self.stop_event.set()

        if self.audio_capture:
            self.audio_capture.stop()

        try:
            self.subtitle_window.close()
        except Exception:
            pass

        try:
            Gtk.main_quit()
        except Exception:
            pass

        return False


# ============================================================
# CLI
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Local English → Ukrainian live subtitles"
    )

    parser.add_argument(
        "--model",
        default="tiny",
        choices=[
            "tiny",
            "base",
            "small",
            "medium",
            "large-v3",
        ],
        help="Whisper model (default: tiny)",
    )

    parser.add_argument(
        "--to",
        default="uk",
        choices=["uk"],
        help="Target language",
    )

    parser.add_argument(
        "--mode",
        default="normal",
        choices=["fast", "normal", "slow"],
        help="Processing mode",
    )

    parser.add_argument(
        "--save",
        default=None,
        help="Save transcript to file",
    )

    parser.add_argument(
        "--gpu",
        action="store_true",
        help="Use CUDA if available",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    translator = LiveTranslator(
        model_name=args.model,
        target_lang=args.to,
        mode=args.mode,
        save_file=args.save,
        use_gpu=args.gpu,
    )

    try:
        translator.start()
    except KeyboardInterrupt:
        translator.stop()
    except Exception as e:
        print(
            f"\n❌ Fatal error: {e}",
            file=sys.stderr,
        )
        translator.stop()
        raise


if __name__ == "__main__":
    main()
