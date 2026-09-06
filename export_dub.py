#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Export The Choicer Voicer dub recordings as MP4 videos.

The game stores:
  - packs_voice/<Pack>/<Scene>/dub_video.ogv        -> the video (original audio, sometimes none)
  - packs_voice/<Pack>/<Scene>/_backing_track.mp3   -> music/SFX without the voices
  - packs_voice/<Pack>/<Scene>/<id>_<Char>.txt|.ini -> dub_timestamps=[seconds]
  - recordings/dub_recordings/<Pack>/<Scene>/<Session>/_dubrecord_<id>_<Char>.wav

This script mixes the backing track + the player's recordings (placed at their
timestamps) over the video, using ffmpeg.

Usage:
  python export_dub.py                 -> interactive menu
  python export_dub.py --list          -> list available sessions
  python export_dub.py -n 3            -> export session #3 from the list
  python export_dub.py --all           -> export every session
Options:
  --game DIR         the game's "game" folder (default: standard install path)
  --out DIR          output folder (default: ./exports next to this script)
  --voice-gain DB    gain applied to voices, in dB (default: 0)
  --music-gain DB    gain applied to the backing track, in dB (default: 0)
  --original-audio   also keep the .ogv's original audio (default: off)
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_GAME_DIR = Path.home() / "AppData/Roaming/YeahMaybe/ChoicerVoicer/game"

RE_TIMESTAMPS = re.compile(r"dub_timestamps\s*=\s*\[([^\]]*)\]")
RE_DUBRECORD = re.compile(r"^_dubrecord_(.+)\.wav$", re.IGNORECASE)


def has_audio_stream(video: Path) -> bool:
    """Some dub_video.ogv files have no audio stream at all."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return bool(result.stdout.strip())
    except OSError:
        return False


def get_duration(media: Path):
    """Duration in seconds (via ffprobe), or None."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(media)],
            capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return float(result.stdout.strip())
    except (OSError, ValueError):
        return None


def _run_with_progress(cmd, total_duration, progress_cb):
    """Run ffmpeg with -progress pipe:1 and report progress (0..1).

    Returns (return_code, last_stderr_line)."""
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                errors="replace") as errf:
        proc = subprocess.Popen(
            cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:],
            stdout=subprocess.PIPE, stderr=errf, text=True,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in proc.stdout:
            line = line.strip()
            # out_time_ms is in microseconds despite its name
            if line.startswith("out_time_ms=") and total_duration:
                try:
                    frac = int(line.split("=", 1)[1]) / 1_000_000 / total_duration
                except ValueError:
                    continue
                if progress_cb:
                    progress_cb(min(max(frac, 0.0), 1.0))
        proc.wait()
        if progress_cb:
            progress_cb(1.0)
        errf.seek(0)
        err = errf.read().strip()
        return proc.returncode, (err.splitlines()[-1] if err else "")


def read_timestamp(scene_dir: Path, line_id: str):
    """Read a line's timestamp (seconds) from its .txt or .ini file (pack age varies)."""
    for ext in (".txt", ".ini"):
        path = scene_dir / f"{line_id}{ext}"
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = RE_TIMESTAMPS.search(content)
        if not m or not m.group(1).strip():
            continue
        try:
            return float(m.group(1).split(",")[0])
        except ValueError:
            continue
    return None


def find_sessions(game_dir: Path):
    """Return sorted [(pack, scene, session_dir, wav_count), ...]."""
    root = game_dir / "recordings" / "dub_recordings"
    sessions = []
    if not root.is_dir():
        return sessions
    for pack_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for scene_dir in sorted(p for p in pack_dir.iterdir() if p.is_dir()):
            for session_dir in sorted(p for p in scene_dir.iterdir() if p.is_dir()):
                wavs = [f for f in session_dir.iterdir()
                        if f.is_file() and RE_DUBRECORD.match(f.name)]
                if wavs:
                    sessions.append((pack_dir.name, scene_dir.name, session_dir, len(wavs)))
    return sessions


def build_export(game_dir: Path, pack: str, scene: str, session_dir: Path,
                 out_dir: Path, voice_gain: float, music_gain: float,
                 keep_original_audio: bool, log=print, progress_cb=None) -> bool:
    scene_dir = game_dir / "packs_voice" / pack / scene
    video = scene_dir / "dub_video.ogv"
    # Packs use various formats for the backing track
    backing = next((p for ext in ("mp3", "ogg", "wav")
                    if (p := scene_dir / f"_backing_track.{ext}").is_file()), None)

    if not video.is_file():
        log(f"  [ERROR] Video not found: {video}")
        return False

    # Match each wav with its timestamp from the pack metadata
    clips = []   # (wav_path, timestamp_seconds)
    missing = []
    for wav in sorted(session_dir.iterdir()):
        m = RE_DUBRECORD.match(wav.name)
        if not m:
            continue
        line_id = m.group(1)                      # ex: "1101_Girl 2"
        ts = read_timestamp(scene_dir, line_id)
        if ts is None:
            missing.append(line_id)
        else:
            clips.append((wav, ts))

    if missing:
        log(f"  [WARNING] {len(missing)} line(s) without timestamp, skipped: "
            + ", ".join(missing[:5]) + ("…" if len(missing) > 5 else ""))
    if not clips:
        log("  [ERROR] No usable recorded lines in this session.")
        return False

    # Build the ffmpeg inputs
    inputs = ["-i", str(video)]
    audio_labels = []   # labels of the streams to mix
    filters = []
    idx = 1

    use_backing = backing is not None
    video_has_audio = has_audio_stream(video)
    if use_backing:
        inputs += ["-i", str(backing)]
        filters.append(f"[{idx}:a]volume={music_gain}dB[music]")
        audio_labels.append("[music]")
        idx += 1
    elif video_has_audio:
        # No backing track: fall back to the .ogv's own audio
        log("  [INFO] No backing track, using the video's own audio.")
        filters.append(f"[0:a]volume={music_gain}dB[music]")
        audio_labels.append("[music]")
    else:
        log("  [INFO] No music track (no backing track, no video audio): voices only.")

    if keep_original_audio and use_backing and video_has_audio:
        filters.append("[0:a]anull[orig]")
        audio_labels.append("[orig]")

    for i, (wav, ts) in enumerate(clips):
        inputs += ["-i", str(wav)]
        delay_ms = max(0, int(round(ts * 1000)))
        filters.append(
            f"[{idx}:a]volume={voice_gain}dB,"
            f"adelay={delay_ms}:all=1[v{i}]"
        )
        audio_labels.append(f"[v{i}]")
        idx += 1

    # apad + -shortest: audio is padded/trimmed to the video's duration,
    # even when the backing track is shorter or missing.
    n = len(audio_labels)
    if n == 1:
        filters.append(f"{audio_labels[0]}alimiter=limit=0.97,apad[aout]")
    else:
        filters.append(
            "".join(audio_labels)
            + f"amix=inputs={n}:duration=longest:normalize=0,"
              "alimiter=limit=0.97,apad[aout]"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*]', "_", f"{scene} - {session_dir.name}")
    out_file = out_dir / f"{safe}.mp4"

    # filter_complex goes through a file to avoid command-line length limits
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        f.write(";\n".join(filters))
        script_path = f.name

    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
           + inputs
           + ["-filter_complex_script", script_path,
              "-map", "0:v", "-map", "[aout]",
              "-c:v", "libx264", "-crf", "18", "-preset", "medium",
              "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "192k",
              "-shortest",
              str(out_file)])

    log(f"  Mixing {len(clips)} lines -> {out_file.name}")
    try:
        if progress_cb is None and log is print:
            returncode = subprocess.run(
                cmd[:1] + ["-stats"] + cmd[1:]).returncode
        else:
            # GUI: progress via -progress, output captured (no console window)
            returncode, err_tail = _run_with_progress(
                cmd, get_duration(video), progress_cb)
            if returncode != 0 and err_tail:
                log("  " + err_tail)
    finally:
        try:
            Path(script_path).unlink()
        except OSError:
            pass

    if returncode != 0:
        log("  [ERROR] ffmpeg failed.")
        return False
    log(f"  OK: {out_file}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Export The Choicer Voicer dubs as MP4 videos.")
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME_DIR)
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parent / "exports")
    parser.add_argument("--list", action="store_true", help="list available sessions")
    parser.add_argument("-n", "--number", type=int, action="append",
                        help="session number(s) to export (repeatable)")
    parser.add_argument("--all", action="store_true", help="export everything")
    parser.add_argument("--voice-gain", type=float, default=0.0)
    parser.add_argument("--music-gain", type=float, default=0.0)
    parser.add_argument("--original-audio", action="store_true",
                        help="also keep the video's original audio")
    args = parser.parse_args()

    if not args.game.is_dir():
        sys.exit(f"Game folder not found: {args.game}")

    sessions = find_sessions(args.game)
    if not sessions:
        sys.exit("No dub recording sessions found.")

    def show_list():
        print("\nAvailable dub sessions:\n")
        for i, (pack, scene, sdir, count) in enumerate(sessions, 1):
            print(f"  {i:3}. [{pack}] {scene}  ({sdir.name}, {count} lines)")
        print()

    if args.list:
        show_list()
        return

    if args.all:
        chosen = list(range(len(sessions)))
    elif args.number:
        chosen = [n - 1 for n in args.number]
        for c in chosen:
            if not (0 <= c < len(sessions)):
                sys.exit(f"Invalid number: {c + 1} (1..{len(sessions)})")
    else:
        show_list()
        raw = input("Session number(s) to export (e.g. 3 or 1,4,7 or 'all'): ").strip()
        if raw.lower() in ("all", "tout", "*"):
            chosen = list(range(len(sessions)))
        else:
            try:
                chosen = [int(x) - 1 for x in re.split(r"[,\s]+", raw) if x]
            except ValueError:
                sys.exit("Invalid input.")
            for c in chosen:
                if not (0 <= c < len(sessions)):
                    sys.exit(f"Invalid number: {c + 1} (1..{len(sessions)})")

    ok = 0
    for c in chosen:
        pack, scene, sdir, count = sessions[c]
        print(f"\n=== [{pack}] {scene} ({sdir.name}) ===")
        if build_export(args.game, pack, scene, sdir, args.out,
                        args.voice_gain, args.music_gain, args.original_audio):
            ok += 1
    print(f"\n{ok}/{len(chosen)} export(s) succeeded. Output folder: {args.out}")


if __name__ == "__main__":
    main()
