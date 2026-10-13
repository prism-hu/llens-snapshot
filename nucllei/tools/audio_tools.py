"""
title: Audio (TTS / STT)
description: 読み上げ (synthesize_speech) と文字起こし (transcribe_speech) の変換ペア。
"""

# =============================================================================
# How it works / constraints
# -----------------------------------------------------------------------------
# nucllei port of owui/tools/audio_tools.py 0.1.0. TTS/STT are built into nucllei natively
# (read-aloud button / mic input), but those are user-driven UI features. This tool exposes the same
# engines as "functions the model can call": a symmetric conversion pair text <=> audio file_id.
#
# synthesize_speech:
#   Reads nucllei's TTS settings (request.app.state.config.TTS_*) and hits tts-bridge's /audio/speech
#   in the same shape as the speech endpoint in routers/audio.py to obtain the audio bytes.
#   The audio is registered as a real file in Storage + Files, and **the embedded player + save link
#   are shown directly to the user via the display channel (__display__, nucllei #88)** — no reliance
#   on the model pasting them into its reply (the #87 approach had a contract gap: the player vanished when the model forgot or altered it).
#   The #87 paste-into-body fallback for old nucllei has been removed (#88 is in every deployment).
#   Since nucllei #87 (2026-07-20) raw `<audio controls>` in the message markdown is rendered as an
#   inline player via sanitize (there is still no rendering path for message attachments —
#   an attachment only shows a FileItem chip, not a playback UI). Also,
#   `[…](URL){download=name}` download-attribute links work (markdown-it-attrs).
#   /api/v1/files/{id}/content serves audio/* inline (formerly: unconditional attachment —
#   Firefox refuses media playback), so it can be used directly as the player's src.
#
# transcribe_speech:
#   Renders the Storage entity of an audio file (file_id) in the chat context to 16kHz mono WAV via ffmpeg
#   and calls transcribe() in routers/audio.py. STT_ENGINE='' so the built-in faster-whisper (CPU)
#   runs. transcribe is synchronous, so it is moved off the event loop with asyncio.to_thread.
#
#   Range selection (start_sec / duration_sec):
#     Passing a long recording whole makes the tool call time out (even past the timeout the cancel
#     never reaches the worker thread, and no partial result comes back). So the tool itself does the
#     cutting with ffmpeg -ss/-t, and caps **the whole of one call** (summed across files, if several)
#     at the valve stt_segment_sec.
#     Why the default is 300 s — the time budget is covered by the timeouts declaration (1800 s, below),
#     but longer ranges degrade quality: within one call,
#     condition_on_previous_text drags misspellings along, and at 600 s every proper noun (STAT6)
#     collapsed to "スタト6". With 300 s x 2 they come out right.
#     -ss goes before -i (input-side fast seek). Odd containers without an index may drift
#     slightly, but avoiding a full decode is worth more.
#     Words on the boundary break because whisper's window is cut (observed: "血圧" -> "血" / "1月"), so
#     the continuation is cut starting stt_overlap_sec earlier. The overlap appears in both ranges.
#
# inspect_audio:
#   Runs only ffprobe and returns length, format and an estimate of the work (est_stt_min). The entry
#   point for deciding "do all of it / which part to take" before transcribing. The Storage entity is
#   probed by its local path, so even large files are never loaded into memory.
#   For a mic-input recording (nucllei attaches it with a segment list in Files meta.dictation),
#   its summary (part/parts/offset_sec, per-segment seconds and transcript) also goes into entry["dictation"],
#   so the model can look up "odd sentence -> around which second" and re-read just that
#   neighbourhood with transcribe_speech's start_sec/duration_sec. Segment seconds are relative to the start
#   of that file (offset_sec = where in the whole recording it begins). Transcripts default to the first 40 chars (with_text=true for full text — to compare against the user's edits);
#   segments can be narrowed with segments_from/segments_to (running index i). joined_text is not returned
#   since it duplicates the utterance in the prompt. For ordinary audio without meta.dictation the entry shape is unchanged.
#
# Audio format handling (ffmpeg 5.1 ships in the image; libmp3lame/libopus/aac enabled):
#   Output — tts-bridge claims Content-Type audio/mpeg but the body is actually raw 44.1kHz 16bit mono
#     WAV (observed: 426KB for 4.8 s). Attaching as-is would be ~5MB per minute, so by default it is
#     converted to mp3 64k mono (observed 1/11). Output format is switched by the output_format valve (wav = no conversion).
#   Input — nucllei's transcribe() calls convert_audio_to_wav only in the STT_ENGINE=="openai"
#     branch; the built-in whisper path passes the path through unconverted. faster-whisper decodes
#     internally with PyAV, so mp3/m4a/ogg/webm etc. pass through, but odd containers or broken headers
#     can fail outright. So by default we normalize to 16kHz mono WAV with ffmpeg before handing it over
#     (also the form whisper wants internally). If normalization fails, the original is passed as-is (fail-soft).
#     This effectively widens accepted input to whatever ffmpeg can decode.
#
# Differences from the owui version:
#  1) transcribe(request, file_path) takes **2 arguments** (owui took 4).
#  2) Files.insert_new_file / get_file_by_id are synchronous; Storage.upload_file takes 2 arguments.
#  3) Synthesized audio is not attached to the message; the player + save link go out on the display channel
#     (an attachment only shows a FileItem chip, with no playback UI).
#  4) TTS_OPENAI_PARAMS does not exist in nucllei, so it is not sent (speed etc. are bridge defaults).
#  5) nucllei's Attachment (markitdown_extract) excludes audio and passes it through unextracted, so
#     this tool is effectively the only path that turns audio into text.
# =============================================================================

import asyncio
import io
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Callable, Optional

import requests
from pydantic import BaseModel, Field

from nucllei.models.files import FileForm, Files
from nucllei.routers.audio import transcribe as _nucllei_transcribe
from nucllei.storage.provider import Storage
from nucllei.utils.ref_errors import file_id_error
from nucllei.utils.tool_errors import NEXT, ToolCallError, ToolError, ToolFailure, ToolFatal, ToolOutcome
from nucllei.utils.tool_output import mark_error_kind

logger = logging.getLogger(__name__)

# Failures raise ToolError (nucllei #145, README_API §5.1 "Failures"). nucllei composes the sentence as
# `ERROR: {fact}。次の手: {next}`. fact is a statement of fact (may be relayed to the user verbatim), next is
# an instruction aimed only at the model. Plain style; never expose internal names (setting names, engine names, ffprobe, Storage).
# A tool not in this toolkit (run_subagent) is mentioned conditionally ("if available").
# kind: bad arguments (seconds format, negative value, empty text, file_id missing/someone else's, requested range cannot be cut) = call /
# no answer (no audio attachment, not audio, out of range, too large) = outcome /
# synthesis unreachable/failed, ffprobe timed out, audio cannot be split / content unreadable, unknown user = failure /
# our side is broken (deployment config = TTS not configured, no request context, ffprobe missing/cannot run,
# empty synthesis response, file read/save failure, unexpected exception in transcription) = fatal.
# A missing file_id and someone else's are not distinguished (do not leak existence; same wording as nucllei's ref_errors.file_id_error).
# Calls that handle several files return, as long as at least one result exists, the failed ones mixed into
# the result as "- name (飛ばした): …" lines. Raise only when there is no result at all (`_all_failed`).
# A result that has content but where part of the job failed (a skipped file / the full text of a long transcript
# could not be kept as .txt) keeps its body and gets the yellow "failure" badge via `mark_error_kind("failure")`
# (nucllei #145, the non-raising variant). Red (fatal) only when things are too broken to return a result. Files
# deferred for lack of budget (deferred) are not failures, so no badge for them.
_RETRY = "少し待って 1 回だけ再試行し、だめならこの事実をユーザーに伝える。"


def _audio_unavailable() -> ToolFatal:
    # Deployment where audio is unavailable (TTS not configured / path without request context). Only operators can
    # fix the cause, so the model gets no breakdown of reasons (setting names, engine names), only the contact.
    return ToolFatal(
        "このサーバーでは音声機能が使えない状態。直せるのは LLENS開発チーム (<redacted-email>) だけ",
        "同じ呼び出しを繰り返さず、この事実をユーザーに伝える。",
    )


def _no_audio() -> ToolOutcome:
    # The target audio is not in the context (shared by inspect_audio / transcribe_speech). Pick-up conditions go in the next step
    return ToolOutcome(
        "この会話に音声の添付が見つからない",
        "添付が無ければ音声ファイルの添付をユーザーに頼む。添付はあるが種類や拡張子で音声と"
        "判別できない形式なら、その file_id を明示して呼び直す (中身で音声か判定する)。",
    )


def _read_failed() -> ToolFatal:
    # The registered file's entity cannot be read from Storage (Storage failure)
    return ToolFatal("ファイルの中身を読み出せない", "添付し直してもらうようユーザーに頼む。")


def _not_audio() -> ToolOutcome:
    return ToolOutcome("音声として読めない (音声ファイルでないか、壊れている)")


def _skipped_line(disp: str, e: ToolError) -> str:
    """One line for a skipped file, mixed into a multi-file result (statement of fact + next step)."""
    return f"- {disp} (飛ばした): {e.fact}。" + (f"{NEXT}{e.next}" if e.next else "")


def _all_failed(failures: list[tuple[str, ToolError]]) -> ToolError:
    """Build the single ToolError to raise when none of the targets produced a result.
    One failure keeps its kind; several with differing kinds become failure."""
    by_kind = {"call": ToolCallError, "failure": ToolFailure, "outcome": ToolOutcome, "fatal": ToolFatal}
    if len(failures) == 1:
        disp, e = failures[0]
        return by_kind[e.kind](f"{disp}: {e.fact}", e.next)
    kinds = {e.kind for _, e in failures}
    nexts = list(dict.fromkeys(e.next for _, e in failures if e.next))
    return by_kind[kinds.pop() if len(kinds) == 1 else "failure"](
        "どのファイルも処理できなかった (" + " / ".join(f"{d}: {e.fact}" for d, e in failures) + ")",
        " ".join(nexts) or None,
    )


def _unlink_quietly(path: Optional[str]) -> None:
    """Clean up a temp file (missing / cannot delete is ignored — does not affect the result)."""
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _report(progress: Optional[Callable[..., None]], msg: str, current=None, total=None) -> None:
    """Progress during execution (nucllei #100, injected `__progress__`). Goes to this tool's own widget.

    No completion summary is sent — nucllei reports elapsed time automatically and the result itself is in the return value."""
    if progress:
        progress(msg, current=current, total=total)

# tts-bridge returns the output of a VOICEVOX-compatible engine (AivisSpeech). It misreports Content-Type as
# audio/mpeg but the body is WAV. We handle raw bytes ourselves, so set it right here.
_SRC_CONTENT_TYPE = "audio/wav"
_SRC_EXT = ".wav"

# Output format of the attached audio. The ffmpeg encoders are confirmed to ship in the image.
# mp3 is the default: its patents expired (2017) so no distribution constraints, and every browser plays it.
_FORMAT_SPEC = {
    "mp3": {"ext": ".mp3", "ct": "audio/mpeg", "codec": "libmp3lame"},
    "opus": {"ext": ".opus", "ct": "audio/ogg", "codec": "libopus"},
    "aac": {"ext": ".m4a", "ct": "audio/mp4", "codec": "aac"},
    "wav": {"ext": _SRC_EXT, "ct": _SRC_CONTENT_TYPE, "codec": None},  # no conversion
}

# Extension hints for auto-detecting audio attachments from the context (fallback when content_type is not
# audio/*). An explicit file_id is not rejected by this list but judged by ffprobe, so the formats actually
# accepted extend to whatever ffmpeg can decode.
_AUDIO_EXTS = {
    "wav", "mp3", "m4a", "mp4", "ogg", "oga", "opus",
    "webm", "flac", "aac", "wma", "amr", "3gp", "aiff", "aif", "caf", "mka", "wv",
}


# -----------------------------------------------------------------------------
# Low-level helpers
# -----------------------------------------------------------------------------
def _read_raw(rec) -> Optional[bytes]:
    """Read raw bytes from the Storage entity (same path as feed_image / sandbox)."""
    path = getattr(rec, "path", None)
    if not path:
        return None
    try:
        local = Storage.get_file(path)
        with open(local, "rb") as f:
            return f.read()
    except Exception as e:  # noqa: BLE001 - None = unreadable (caller turns it into _read_failed)
        logger.error(f"[audio_tools] raw read failed: {e!r}")
        return None


def _local_path(rec) -> Optional[str]:
    """Return the local path of the Storage entity (a read path that avoids loading bytes)."""
    path = getattr(rec, "path", None)
    if not path:
        return None
    try:
        return Storage.get_file(path)
    except Exception as e:  # noqa: BLE001 - None makes _materialize fall back to raw bytes
        logger.error(f"[audio_tools] storage path resolve failed: {e!r}")
        return None


def _materialize(rec, display_name: str) -> tuple[Optional[str], bool]:
    """Make the audio a local path readable by ffmpeg/ffprobe and return (path, is_temp_file).

    Normally the Storage entity's path is used as-is (raw bytes are never read even for long recordings).
    In setups where the path cannot be resolved, raw bytes are dumped to a temp file as before.
    When the second element is True the caller is responsible for deleting it."""
    p = _local_path(rec)
    if p and os.path.exists(p):
        return p, False
    raw = _read_raw(rec)
    if raw is None:
        return None, False
    ext = os.path.splitext(display_name)[1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
        f.write(raw)
        return f.name, True


def _register_binary_file(
    user_id: str, display_name: str, data: bytes, content_type: str
) -> str:
    """Register binary data as a real file in Storage + Files and return the file_id."""
    file_id = str(uuid.uuid4())
    stored_name = f"{file_id}_{display_name}"
    _contents, path = Storage.upload_file(io.BytesIO(data), stored_name)
    Files.insert_new_file(
        user_id,
        FileForm(
            **{
                "id": file_id,
                "filename": display_name,
                "path": path,
                "data": {},
                "meta": {
                    "name": display_name,
                    "content_type": content_type,
                    "size": len(data),
                },
            }
        ),
    )
    return file_id




def _iter_context_files(__files__, __metadata__):
    """Enumerate chat-context file references passed to the tool as (file_id, filename, ref)."""
    raw = []
    if __files__:
        raw = __files__
    elif __metadata__ and __metadata__.get("files"):
        raw = __metadata__["files"]
    for f in raw or []:
        inner = f.get("file", {}) if isinstance(f, dict) else {}
        fid = (f.get("id") if isinstance(f, dict) else None) or inner.get("id")
        fname = (f.get("name") if isinstance(f, dict) else None) \
            or inner.get("filename") or (inner.get("meta") or {}).get("name")
        if fid:
            yield fid, fname, f


def _resolve_targets(
    file_id: Optional[str],
    filename: Optional[str],
    __files__,
    __metadata__,
) -> list[tuple[str, Optional[str]]]:
    """Decide the (file_id, filename) targets: explicit > audio attachments in the context (filtered by filename).

    An explicit target is not rejected by extension/content_type (ffprobe downstream judges by the real stream)."""
    if file_id:
        return [(file_id, None)]
    targets = []
    for fid, fname, ref in _iter_context_files(__files__, __metadata__):
        ct = ((ref.get("file", {}) if isinstance(ref, dict) else {}).get("meta") or {}).get(
            "content_type"
        )
        # API clients may send files as just {type, id} (the UI includes file.filename /
        # file.meta). When neither name nor content_type is present, fill in from the Files record —
        # otherwise an audio attachment turns into "no target" (found in the staging check on 2026-10-03).
        if not fname or not ct:
            try:
                rec = Files.get_file_by_id(fid)
            except Exception as e:  # noqa: BLE001 - just cannot fill in (judgement continues with the name/type at hand)
                logger.warning(f"[audio_tools] file record lookup failed for {fid}: {e!r}")
                rec = None
            if rec:
                fname = fname or getattr(rec, "filename", None)
                ct = ct or ((getattr(rec, "meta", None) or {}).get("content_type"))
        if not _is_audio(fname, ct):
            continue
        if filename and (not fname or filename.lower() not in fname.lower()):
            continue
        targets.append((fid, fname))
    return targets


def _is_audio(fname: Optional[str], content_type: Optional[str]) -> bool:
    # video/webm counts too: the browser file picker sends .webm audio as video/webm
    # (same treatment as nucllei's skill auto-load condition). Whether audio is really there is up to ffprobe.
    ct = (content_type or "").lower()
    if ct.startswith("audio/") or ct == "video/webm":
        return True
    if fname:
        ext = os.path.splitext(fname)[1].lower().lstrip(".")
        return ext in _AUDIO_EXTS
    return False


_DICTATION_TEXT_HEAD = 40


def _dictation_summary(
    rec,
    with_text: bool = False,
    seg_from: Optional[int] = None,
    seg_to: Optional[int] = None,
) -> Optional[dict]:
    """Summarize Files meta.dictation (a mic-input recording) for inspect_audio. None if absent.

    Seconds (start_sec / end_sec) are returned relative to the start of that file. text is the head only with
    with_text=False, full with True. seg_from / seg_to are a range of the running index i (inclusive)."""
    meta = getattr(rec, "meta", None) or {}
    d = meta.get("dictation") if isinstance(meta, dict) else None
    if not isinstance(d, dict):
        return None
    segs = []
    for sg in d.get("segments") or []:
        if not isinstance(sg, dict):
            continue
        i = sg.get("i")
        if (seg_from is not None or seg_to is not None) and not isinstance(i, int):
            continue
        if seg_from is not None and i < seg_from:
            continue
        if seg_to is not None and i > seg_to:
            continue
        text = sg.get("text") or ""
        if not with_text and len(text) > _DICTATION_TEXT_HEAD:
            text = text[:_DICTATION_TEXT_HEAD] + "…"
        segs.append(
            {
                "i": i,
                "start_sec": sg.get("start_sec"),
                "end_sec": sg.get("end_sec"),
                "cut": sg.get("cut"),
                "text": text,
            }
        )
    return {
        "part": d.get("part"),
        "parts": d.get("parts"),
        "offset_sec": d.get("offset_sec"),
        "duration_sec": d.get("duration_sec"),
        "segments": segs,
    }


# -----------------------------------------------------------------------------
# ffmpeg (all synchronous; the caller moves them to asyncio.to_thread)
# -----------------------------------------------------------------------------
def _run(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, timeout=timeout, check=True
    )


def _encode_audio(raw: bytes, fmt: str, bitrate: str, timeout: int) -> tuple[bytes, str, str]:
    """Convert WAV bytes to fmt and return (bytes, ext, content_type).
    For fmt='wav' or on conversion failure the original is returned as-is (fail-soft — heavier beats silent)."""
    spec = _FORMAT_SPEC.get(fmt) or _FORMAT_SPEC["wav"]
    if not spec["codec"]:
        return raw, spec["ext"], spec["ct"]
    src = dst = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=_SRC_EXT) as f:
            f.write(raw)
            src = f.name
        dst = src + spec["ext"]
        _run(
            ["ffmpeg", "-v", "error", "-y", "-i", src,
             "-ac", "1", "-c:a", spec["codec"], "-b:a", bitrate, dst],
            timeout,
        )
        with open(dst, "rb") as f:
            out = f.read()
        if not out:
            raise RuntimeError("conversion result is empty")
        return out, spec["ext"], spec["ct"]
    except Exception as e:  # noqa: BLE001 - fail-soft (heavier beats silent). Reason goes to the log
        logger.warning(f"[audio_tools] encode to {fmt} failed: {e!r} → attaching WAV as-is")
        return raw, _SRC_EXT, _SRC_CONTENT_TYPE
    finally:
        _unlink_quietly(src)
        _unlink_quietly(dst)


def _probe_audio(path: str, timeout: int = 30) -> dict:
    """Return the audio stream's metadata via ffprobe (authoritative judgement independent of extension).
    No audio stream / ffprobe cannot read the content (non-zero exit, not JSON) -> empty dict = "not audio".
    ffprobe cannot be launched (deployment error) is ToolFatal, timeout is ToolFailure — neither is a problem
    with the content, so we do not say it is not audio.

    duration falls back to the format side because some containers (webm/ogg etc.) lack it on the stream side.
    None if neither has it (length unknown; ranges still work but the remainder cannot be reported)."""
    try:
        p = _run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=codec_name,sample_rate,channels,duration"
             ":format=duration,format_name,bit_rate",
             "-of", "json", path],
            timeout,
        )
        data = json.loads(p.stdout or b"{}") or {}
    except (subprocess.CalledProcessError, ValueError) as e:  # cannot read the content = not audio
        logger.info(f"[audio_tools] ffprobe could not read the file: {e!r}")
        return {}
    except subprocess.TimeoutExpired as e:  # timed out
        logger.error(f"[audio_tools] ffprobe timed out: {e!r}")
        raise ToolFailure("音声の長さ・形式を調べられなかった", _RETRY)
    except OSError as e:  # ffprobe missing / cannot run
        logger.error(f"[audio_tools] ffprobe failed to run: {e!r}")
        raise ToolFatal("音声の長さ・形式を調べられなかった", _RETRY)
    streams = data.get("streams") or []
    if not streams:
        return {}
    st, fmt = streams[0], (data.get("format") or {})

    def _num(v, cast=float):
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    return {
        "codec": st.get("codec_name"),
        "sample_rate": _num(st.get("sample_rate"), int),
        "channels": _num(st.get("channels"), int),
        "container": fmt.get("format_name"),
        "bit_rate": _num(fmt.get("bit_rate"), int),
        "duration_sec": _num(st.get("duration")) or _num(fmt.get("duration")),
    }


def _hms(sec: Optional[float]) -> str:
    """Seconds to H:MM:SS. Unknown (None) is '?'."""
    if sec is None:
        return "?"
    total = max(0, int(round(sec)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _as_sec(v: Any, label: str) -> Optional[float]:
    """Normalize a seconds value to float. Accepts 600 / "600" / "10:00" / "1:02:03".
    Unspecified is None. An uninterpretable value is ToolCallError — silently treating it as 0 would
    cause "meant to continue but re-transcribed from the start" accidents."""
    how = '秒数 (例: 600) か "10:00" の形で渡す。'
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise ToolCallError(f"{label} が秒数でない (指定: {v!r})", how)
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        if ":" in s:
            total = 0.0
            for part in s.split(":"):
                total = total * 60 + float(part)
            return total
        return float(s)
    except ValueError:
        raise ToolCallError(f"{label} を秒数として解釈できない (指定: {v!r})", how)


def _normalize_to_wav(
    src: str,
    timeout: int,
    start_sec: float = 0.0,
    duration_sec: Optional[float] = None,
) -> Optional[str]:
    """Return the path of a temp file normalized to the form whisper uses internally (16kHz mono PCM WAV).
    Passing start_sec / duration_sec cuts out just that range.
    None on failure (without a range the caller passes the original as-is).

    Output always goes to a temp directory — src may be the Storage entity's path, so
    nothing is written next to it."""
    fd, dst = tempfile.mkstemp(suffix=".norm.wav")
    os.close(fd)
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if start_sec and start_sec > 0:
        # before -i = input-side fast seek. No need to decode from the start (matters for long files)
        cmd += ["-ss", f"{start_sec:.3f}"]
    cmd += ["-i", src]
    if duration_sec and duration_sec > 0:
        cmd += ["-t", f"{duration_sec:.3f}"]
    cmd += ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", dst]
    try:
        _run(cmd, timeout)
        if os.path.getsize(dst) > 0:
            return dst
        raise RuntimeError("conversion result is empty")
    except Exception as e:  # noqa: BLE001 - the caller sees None and either falls back to the original or fails (reason goes to the log)
        logger.warning(f"[audio_tools] normalize failed: {e!r}")
        _unlink_quietly(dst)
        return None


# -----------------------------------------------------------------------------
# Tool body
# -----------------------------------------------------------------------------
class Tools:
    # llens(#110): opt-in to asynchronous execution (promotion to background). STT is slow
    # (<redacted> measured 2026-08-14), so long audio can be made background.
    # TTS can also take minutes for long text, so it is included. inspect_audio only
    # runs ffprobe and is instant, so it is excluded.
    backgroundable = ("synthesize_speech", "transcribe_speech")

    # llens(#110 P4): per-entry timeout (nucllei's `timeouts` class attribute). 1800 s for both sync
    # and background (same as the ASYNC_EXEC_TIMEOUT_S default). Before this declaration the sync default of 300 s
    # sometimes killed ranges near the stt_segment_sec cap. Keeping stt_segment_sec=300 is
    # about quality (the condition_on_previous_text observation in the header comment), not the time budget.
    timeouts = {"transcribe_speech": 1800}

    class Valves(BaseModel):
        tts_timeout_sec: float = Field(
            default=60.0,
            description="tts-bridge への合成リクエストのタイムアウト (秒)。",
        )
        max_tts_chars: int = Field(
            default=2000,
            description="1 回の synthesize_speech で合成する最大文字数。超過分は切り詰める。",
        )
        stt_max_mb: float = Field(
            default=2000.0,
            description=(
                "transcribe_speech が受け付ける音声ファイルの上限サイズ (MB)。"
                "実行時間は stt_segment_sec (長さ) で守るので、ここは異常なファイルを "
                "弾くだけのサニティ用。区間切り出しは ffmpeg が必要な範囲しか読まないため、"
                "大きいファイル自体は問題にならない。"
            ),
        )
        stt_segment_sec: float = Field(
            default=300.0,
            description=(
                "transcribe_speech の **1 回の呼び出し全体** が処理する最大音声長 (秒)。"
                "対象が複数ファイルでも合算でこの上限に収める。既定 300 秒の根拠: 時間は "
                "timeouts 宣言 (1800 秒) で足りる (実測 RTF 0.22〜0.40 で 300 秒音声は 120 秒以内) が、"
                "区間を 600 秒に伸ばすと同一呼び出し内の condition_on_previous_text が誤表記を"
                "引きずり固有名詞が崩れた (実測)。伸ばすなら品質を再確認すること。"
                "0 = 無制限 (タイムアウトを自分で管理する場合のみ)。"
            ),
        )
        stt_rtf_estimate: float = Field(
            default=0.25,
            description=(
                "inspect_audio が返す所要時間の見積りに使う RTF (処理時間 ÷ 音声長)。"
                "この機の実測: 密な日本語発話 0.22 / 話者重なり 0.40 / 無音 0.002。"
                "見積りにしか使わない (実行の制御は stt_segment_sec)。"
            ),
        )
        stt_overlap_sec: float = Field(
            default=3.0,
            description=(
                "続きを書き起こす時に、指定位置より少し手前から切り出す秒数。"
                "区間の境界で語が壊れる (実測: 「血圧」→ 前半「血」/ 後半「1月」) のを防ぐ。"
                "重複した数秒ぶんのテキストが両区間に出るので、0 にすれば重複は消えるが "
                "境界の語は壊れる。"
            ),
        )
        max_transcript_chars: int = Field(
            default=20_000,
            description=(
                "文字起こし結果を tool result に載せる上限 (文字)。超過分は切り詰めず、"
                "全文を .txt として登録し冒頭のみ返す (文字起こしは切り詰めると用を成さないため)。"
                "0 = 無効 (常に全文を返す)"
            ),
        )
        output_format: str = Field(
            default="mp3",
            description=(
                "添付する合成音声の形式: mp3 / opus / aac / wav。"
                "mp3 は特許失効済みで全ブラウザ再生可のため既定。opus は最小 (Safari 要注意)。"
                "wav は無変換 (最大)。変換に失敗した場合は WAV のまま添付する。"
            ),
        )
        output_bitrate: str = Field(
            default="64k",
            description="合成音声の音声ビットレート (モノラル)。opus は 32k でも十分。",
        )
        normalize_input: bool = Field(
            default=True,
            description=(
                "文字起こし前に ffmpeg で 16kHz mono WAV へ正規化する。"
                "ffmpeg が decode できる形式なら概ね受け付けられるようになる。"
                "失敗時は原本のまま whisper に渡す (fail-soft)。"
            ),
        )
        ffmpeg_timeout_s: int = Field(
            default=120, description="ffmpeg / ffprobe 1 回あたりのタイムアウト (秒)。"
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    # =========================================================================
    # synthesize_speech (TTS)
    # =========================================================================
    async def synthesize_speech(
        self,
        text: str,
        voice: Optional[str] = None,
        __request__: Any = None,
        __user__: Optional[dict] = None,
        __progress__: Optional[Callable[..., None]] = None,
        __display__: Optional[Callable[..., None]] = None,
    ) -> str:
        """テキストを音声に合成し、再生プレイヤーをユーザーへ表示する。

        - プレイヤーと保存リンクはツールが表示する。本文に貼り直さず、音声の内容も
          復唱しない。
        - 読み上げに適した自然な文にしてから渡す (記号の羅列やコードブロックは不向き)。
        - 1 回に渡せる文字数には上限がある (既定 2000 字)。超えた分は合成されない
          (戻り値が申告する)。長い文章は段落ごとに分けて複数回呼ぶ。
        - 声は既定のままでよい (voice は通常省略)。

        :param text: 読み上げるテキスト (日本語想定)。
        :param voice: (任意) 声の style ID (整数文字列)。ユーザーが番号を指定したときだけ渡す。省略時は既定の声。
        :return: 表示済みの旨の要約。失敗は ToolError を raise。
        """
        if __request__ is None:
            raise _audio_unavailable()
        if not __user__ or not __user__.get("id"):
            raise ToolFailure("ユーザーを特定できない", "呼び直さず、この事実をユーザーに伝える。")
        text = (text or "").strip()
        if not text:
            raise ToolCallError("text が空", "読み上げる文字列を渡す。")
        orig_chars = len(text)
        if orig_chars > self.valves.max_tts_chars:
            text = text[: self.valves.max_tts_chars]

        cfg = __request__.app.state.config
        engine = getattr(cfg, "TTS_ENGINE", "")
        base_url = getattr(cfg, "TTS_OPENAI_API_BASE_URL", "") or ""
        if engine != "openai" or not base_url:
            logger.warning(f"[audio_tools] TTS not configured for the bridge: engine={engine!r} base_url={base_url!r}")
            raise _audio_unavailable()

        payload = {
            "model": getattr(cfg, "TTS_MODEL", None) or "voicevox-v1",
            "input": text,
            "voice": voice or getattr(cfg, "TTS_VOICE", None),
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {getattr(cfg, 'TTS_OPENAI_API_KEY', '') or 'EMPTY'}",
        }
        url = f"{base_url}/audio/speech"

        _report(__progress__, "音声を合成中...")
        try:
            audio = await asyncio.to_thread(
                self._post_tts, url, payload, headers, self.valves.tts_timeout_sec
            )
        except requests.RequestException as e:  # connection failure / timeout / 4xx/5xx from the bridge
            logger.error(f"[audio_tools] synthesize_speech synth failed: {e!r}")
            # The exception text contains the TTS bridge's internal URL, so do not show it (full text in the log)
            raise ToolFailure("音声合成に失敗した", _RETRY)

        if not audio:
            raise ToolFatal("音声合成の結果が空だった", _RETRY)

        # The bridge output is raw WAV. Convert to mp3 by default before attaching (WAV is ~5MB per minute).
        raw_bytes = len(audio)
        audio, ext, content_type = await asyncio.to_thread(
            _encode_audio,
            audio,
            (self.valves.output_format or "mp3").lower(),
            self.valves.output_bitrate,
            self.valves.ffmpeg_timeout_s,
        )

        name = f"speech_{uuid.uuid4().hex[:8]}{ext}"
        try:
            fid = _register_binary_file(__user__["id"], name, audio, content_type)
        except Exception as e:  # noqa: BLE001 - Storage / DB exceptions contain internal paths, so fixed wording and log it
            logger.error(f"[audio_tools] synthesize_speech register failed: {e!r}")
            raise ToolFatal("合成音声の保存に失敗した", _RETRY)

        fmt = ext.lstrip(".")
        kb = len(audio) / 1024
        url = f"/api/v1/files/{fid}/content"
        # The blank line between the player (html_block) and the save link is required —
        # without it the link line is swallowed into the HTML block and not rendered as markdown
        player = f'<audio controls src="{url}"></audio>'
        dl = f"[🔊 音声を保存 ({fmt}, {kb:.0f}KB)]({url}){{download={name}}}"
        cut = "" if orig_chars == len(text) else f"/{orig_chars} ※超過分は未合成"
        summary = (
            f"OK: 音声を生成した "
            f"(file_id={fid}, chars={len(text)}{cut}, voice={payload['voice']}, "
            f"{fmt} {kb:.0f}KB / 変換前 WAV {raw_bytes / 1024:.0f}KB)。"
        )
        # nucllei #88: send the player + save link straight to the user on the display channel.
        # No reliance on the model pasting them into its reply — closes the #87 contract gap where the player
        # vanished when the model forgot or altered it. The return value is just a "displayed" summary so the model does not re-emit it.
        # #88 is in every deployment (injected-parameter table in README_API §5.3), so the paste-into-body
        # fallback for old nucllei was removed. __display__ is absent only on non-chat paths (headless API),
        # where there is nowhere to display — do not make the model paste; return only the file_id.
        if __display__ is None:
            return f"{summary} この経路では再生プレイヤーを表示できない。"
        __display__(f"{player}\n\n{dl}")
        return (
            f"{summary} 再生プレイヤーと保存リンクはユーザーの画面に表示済み。"
            f"{NEXT}本文で音声の内容を復唱せず、プレイヤーやリンクも貼り直さない。"
        )

    @staticmethod
    def _post_tts(url: str, payload: dict, headers: dict, timeout: float) -> bytes:
        """Synchronous POST to tts-bridge, returning audio bytes (called from asyncio.to_thread)."""
        r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.content

    # =========================================================================
    # inspect_audio (length / format only; no transcription)
    # =========================================================================
    async def inspect_audio(
        self,
        file_id: Optional[str] = None,
        filename: Optional[str] = None,
        with_text: bool = False,
        segments_from: Optional[int] = None,
        segments_to: Optional[int] = None,
        __user__: Optional[dict] = None,
        __files__: Optional[list] = None,
        __metadata__: Optional[dict] = None,
    ) -> dict:
        """音声ファイルの長さ・形式を、文字起こしせずに調べる (すぐ終わる)。

        **長さが分からない音声は、transcribe_speech の前にこれを呼ぶ。** 文字起こしは
        1 回あたり数分ぶん (既定 300 秒) ずつなので、長い録音ほど回数がかかる。
        「この録音は何分?」のような長さ自体の質問にも、文字起こしせずこれで答える。

        マイク入力の録音 (`<source dictation="true">`) なら、区間一覧も `dictation` に返る。
        おかしい文がどの秒にあるかはこれで引き、transcribe_speech の start_sec /
        duration_sec でその前後だけ読み直す。
        - 区間の秒は**そのファイルの先頭基準**。そのまま transcribe_speech に渡す (offset_sec は
          足さない)。offset_sec はそのファイルが録音全体の何秒目から始まるか — 利用者が録音全体の
          時刻で場所を言ったときだけ、それから offset_sec を引いてファイル内の秒にする。
          分割録音のどのファイルかは、各 part の segments の転写と発言の文を照らして選ぶ。
        - dictation.duration_sec はそのファイル (part) の長さで、録音全体ではない。
        - cut: forced = 上限で強制的に切れた (境目で語が割れやすい) / breath・silence = 息継ぎ・
          無音で切れた / end = 最後の区間。text が空 = 転写されなかった区間 (発言の文に入っていない)。
        - 長い録音では区間一覧が大きいので segments_from / segments_to で絞る。
        - 利用者が送信前に手直ししたかを発言の文と見比べるときは with_text=true (区間の転写を全文で返す)。
        - 戻り値は {"segment_sec", "files": [ファイルごとの情報], "hint"}。長さを持たないコンテナでは
          duration / start_secs / est_stt_min が欠ける (その時は start_sec=0 から呼び、transcribe_speech の戻り値末尾の「次の手:」で
          続きを辿る)。est_stt_min は単独実行の見積りで、他の文字起こしと重なると伸びる。
          一部のファイルだけ調べられなかったときは、そのファイルの情報が `error` (事実の文 +「次の手:」) になる。

        :param file_id: (任意) 調べる音声の file_id を直接指定。
        :param filename: (任意) 対象ファイル名の部分一致フィルタ。省略時は音声添付全件。
        :param with_text: (任意) true で区間ごとの転写を全文で返す (既定は冒頭 40 字)。
        :param segments_from: (任意) 返す区間の通し番号 i の下限 (含む)。省略で先頭から。
        :param segments_to: (任意) 返す区間の通し番号 i の上限 (含む)。省略で末尾まで。
        :return: {"segment_sec", "files": [ファイルごとの情報], "hint"}。
                 長さを持たないコンテナでは duration / start_secs / est_stt_min が欠ける
                 (その時は start_sec=0 から呼び、transcribe_speech の戻り値末尾の「次の手:」で続きを辿る)。
                 est_stt_min は単独実行の見積り — 他の文字起こしと重なると伸びる。
                 マイク入力の録音では各ファイルに "dictation": {"part", "parts",
                 "offset_sec", "duration_sec", "segments": [{"i", "start_sec",
                 "end_sec", "cut", "text" (既定は冒頭 40 字)}]} が付く。
        """
        targets = _resolve_targets(file_id, filename, __files__, __metadata__)
        if not targets:
            raise _no_audio()

        cap = self.valves.stt_segment_sec
        out = []
        failures: list[tuple[str, ToolError]] = []

        def skip(entry: dict, e: ToolError) -> None:
            # Per-file failures go into the entry as fact + next step (raise later if all failed)
            entry["error"] = f"{e.fact}。" + (f"{NEXT}{e.next}" if e.next else "")
            out.append(entry)
            failures.append((entry["filename"] or entry["file_id"], e))

        for fid, fname in targets:
            entry: dict[str, Any] = {"file_id": fid, "filename": fname}
            rec = Files.get_file_by_id(fid)
            # Owner check (closes the same class of IDOR as #40): same shape as feed_image / transcribe.
            # Missing and someone else's get the same wording (do not leak existence)
            owner = getattr(rec, "user_id", None) if rec else None
            if not rec or owner != (__user__ or {}).get("id"):
                skip(entry, file_id_error(fid))
                continue
            disp = fname or getattr(rec, "filename", None) or fid
            entry["filename"] = disp
            src, is_tmp = _materialize(rec, disp)
            if not src:
                skip(entry, _read_failed())
                continue
            try:
                size = os.path.getsize(src)
                probe = await asyncio.to_thread(
                    _probe_audio, src, self.valves.ffmpeg_timeout_s
                )
            except ToolError as e:  # ffprobe does not work (_probe_audio)
                skip(entry, e)
                continue
            finally:
                if is_tmp:
                    _unlink_quietly(src)
            if not probe:
                skip(entry, _not_audio())
                continue
            dur = probe.get("duration_sec")
            entry.update(
                {
                    "duration": _hms(dur),
                    "duration_sec": round(dur, 1) if dur is not None else None,
                    "size_mb": round(size / 1024 / 1024, 1),
                    "codec": probe.get("codec"),
                    "sample_rate": probe.get("sample_rate"),
                    "channels": probe.get("channels"),
                    "container": probe.get("container"),
                }
            )
            if dur and cap > 0:
                # Do not include a position exactly at the end (if mp3 padding etc. makes the real length
                # 1800.05 s, ceil would run one extra time and add a wasted call).
                # The start is always needed though (audio under 1 s is still transcribed in one call)
                starts = [
                    int(i * cap)
                    for i in range(max(1, math.ceil(dur / cap)))
                    if i == 0 or i * cap < dur - 1
                ]
                entry["transcribe_calls_needed"] = len(starts)
                entry["start_secs"] = starts
            else:
                entry["transcribe_calls_needed"] = 1 if dur is not None else None
            if dur:
                # Rough time to transcribe all of it. Material for deciding whether to consult the user
                # (RTF is measured on this machine. Recordings with overlapping speakers take nearly twice as long)
                entry["est_stt_min"] = round(dur * self.valves.stt_rtf_estimate / 60, 1)
            dictation = _dictation_summary(rec, with_text, segments_from, segments_to)
            if dictation:
                entry["dictation"] = dictation
            out.append(entry)

        if len(failures) == len(out):
            # No file could be inspected = nothing to return
            raise _all_failed(failures)
        if failures:
            mark_error_kind("failure")  # some files could not be inspected

        hint = (
            f"{NEXT}用件に要る範囲だけ start_sec で取る。全部読むなら start_secs の各値を "
            "1 回ずつ渡す (全体で est_stt_min 分かかる)。"
        )
        if any("dictation" in e for e in out):
            hint += (
                " dictation.segments の start_sec / end_sec はそのファイルの先頭基準の秒 — "
                "読み直す区間はその値のまま transcribe_speech に渡す (offset_sec は足さない。"
                "利用者が録音全体の時刻を言ったときだけ offset_sec を引く)。"
            )
        return {
            "segment_sec": cap,
            "files": out,
            "hint": hint,
        }

    # =========================================================================
    # transcribe_speech (STT)
    # =========================================================================
    async def transcribe_speech(
        self,
        file_id: Optional[str] = None,
        filename: Optional[str] = None,
        start_sec: Optional[float] = None,
        duration_sec: Optional[float] = None,
        __request__: Any = None,
        __user__: Optional[dict] = None,
        __files__: Optional[list] = None,
        __metadata__: Optional[dict] = None,
        __progress__: Optional[Callable[..., None]] = None,
    ) -> str:
        """添付された音声ファイルを文字起こしして本文テキストを返す。

        あなたは音声を直接聞けないので、添付音声の内容を問われたらこれでテキスト化する。

        - 引数を省略するとこの会話の音声添付が対象になる (filename で絞り込める)。
          file_id を直接渡した場合は拡張子でなく実際の音声ストリームで判定するので、
          珍しい形式や拡張子の無いファイルでも通る。
        - 1 回で処理するのは数分ぶん (対象が複数なら合算)。超えた分は処理されないので、
          続きの取り方を戻り値の末尾から読んで呼び直す。長さが分からない音声は先に
          inspect_audio で規模を見る (すぐ終わる)。
        - 出るのは素のテキストだけ。**話者の区別も時刻も付かず、言語は日本語に固定**。
          固有名詞・薬剤名は崩れる (実測: STAT6 →「スタト6」) ので、そのまま記録に使わせない。
        - 続きの区間は先頭数秒が前の区間と重複する (境界で語が壊れるのを防ぐため)。
          繋げる時は重複を落とす。
        - 切り出しはツール側でやる。自分で分割しない。
        - **文字起こしは遅い** (実時間の数分の一程度でしか進まない)。長尺 (目安 10 分超)
          や続き区間の連続取得は `background: true` で非同期にし、親ターンを塞がない
          (完了結果はサーバが自動で届ける)。
        - 続きを取る時は、前回の戻り値が示した start_sec をそのまま渡し、file_id で 1 件に絞る
          (複数ファイルが対象だと全部に同じ位置が効く)。
        - duration_sec は終了位置ではなく**長さ** — 10:00〜12:00 が欲しければ start_sec=600,
          duration_sec=120。省略時・上限超は 1 回の上限まで。
        - 極端に長い結果は冒頭だけが返り、全文は .txt として登録されて file_id が示される。
        - 対象が複数で一部だけ処理できなかったときは、その分が `- 名前 (飛ばした): …` の行で結果に混ざる。

        :param file_id: (任意) 文字起こしする音声の file_id を直接指定。
        :param filename: (任意) 対象ファイル名の部分一致フィルタ。省略時は音声添付全件。
        :param start_sec: (任意) 音声の先頭から数えた開始位置 (秒)。省略時は 0 = 冒頭から。
        :param duration_sec: (任意) 処理する長さ (秒)。終了位置ではない。
        :return: 文字起こし結果テキスト (区間と残量の注記つき)。
                 極端に長い場合だけ冒頭のみが返り、全文は .txt として登録され file_id が示される。
                 失敗は ToolError を raise (対象のどれからも結果が出なかったときだけ。
                 一部だけ失敗したときは、その分を「- 名前 (飛ばした): …」の行で結果に混ぜる)。
        """
        if __request__ is None:
            raise _audio_unavailable()

        start = _as_sec(start_sec, "start_sec") or 0.0
        want = _as_sec(duration_sec, "duration_sec")
        if start < 0:
            raise ToolCallError("start_sec が負の値", "0 以上の秒数で渡す。")
        if want is not None and want <= 0:
            raise ToolCallError("duration_sec が 0 以下", "正の秒数で渡す (省略すれば 1 回の上限まで)。")

        # Resolve the target file_ids: explicit > audio attachments in the context (filtered by filename)
        # explicit=True targets are not rejected by extension/content_type; ffprobe decides whether they are audio.
        explicit = bool(file_id)
        targets = _resolve_targets(file_id, filename, __files__, __metadata__)

        if not targets:
            raise _no_audio()

        # Budget of audio length processed by the whole call. Kept as a sum even with several targets
        # (a per-file cap would hit the tool execution timeout with two attached recordings)
        cap = self.valves.stt_segment_sec
        budget = cap if cap > 0 else 0.0
        deferred = []

        results = []
        failures: list[tuple[str, ToolError]] = []

        def skip(disp: str, e: ToolError) -> None:
            results.append(_skipped_line(disp, e))
            failures.append((disp, e))

        for idx, (fid, fname) in enumerate(targets, start=1):
            rec = Files.get_file_by_id(fid)
            # Owner check (closes the same class of IDOR as #40): do not transcribe another user's audio via
            # an explicit file_id (context attachments are the user's own, so they pass). Same shape as feed_image.py.
            # Missing and someone else's get the same wording, and the other person's filename is not shown (do not leak existence)
            owner = getattr(rec, "user_id", None) if rec else None
            if not rec or owner != (__user__ or {}).get("id"):
                skip(fname or fid, file_id_error(fid))
                continue
            meta = getattr(rec, "meta", None) or {}
            disp = fname or getattr(rec, "filename", None) or fid
            # Only targets picked up from the context are screened by extension/content_type. An explicit file_id
            # is not rejected here; the judgement is left to ffprobe downstream (presence of a real stream).
            if not explicit and not _is_audio(disp, meta.get("content_type")):
                skip(disp, ToolOutcome("音声ファイルではない"))
                continue
            # If extracted text already exists, use it and avoid a second whisper run.
            # The current Attachment (markitdown_extract) excludes audio and passes it through unextracted,
            # so this is normally empty. It matters if swapped for an Attachment that handles audio.
            # Not usable with a range (the extracted text covers the whole and cannot be cut).
            cached = ((getattr(rec, "data", None) or {}).get("content") or "").strip()
            if cached and not start and want is None:
                results.append(self._emit(disp, cached, "", disp, __user__, 0.0))
                continue
            # Once the budget is used up, the remaining files are only listed, not processed
            if cap > 0 and budget < min(30.0, cap):
                deferred.append(f"{disp} (file_id={fid})")
                continue
            # Only what reaches here actually runs whisper (no "transcribing" for skipped targets).
            # The count is shown only with several files — in the main use case, "one long recording
            # split into ranges", len(targets)==1 and showing it would look like 1/1 = done for minutes.
            _report(
                __progress__,
                f"文字起こし中: {disp}" + (f" ({_hms(start)}〜)" if start else ""),
                current=idx if len(targets) > 1 else None,
                total=len(targets) if len(targets) > 1 else None,
            )
            src, is_tmp = _materialize(rec, disp)
            if not src:
                skip(disp, _read_failed())
                continue
            try:
                size = os.path.getsize(src)
                if size > self.valves.stt_max_mb * 1024 * 1024:
                    skip(disp, ToolOutcome(
                        f"大きすぎて文字起こしできない ({size / 1024 / 1024:.1f}MB、"
                        f"上限 {self.valves.stt_max_mb}MB)"
                    ))
                    continue
                info = await asyncio.to_thread(
                    self._run_stt, __request__, src,
                    self.valves.normalize_input, self.valves.ffmpeg_timeout_s,
                    start, want, budget if cap > 0 else 0.0,
                    self.valves.stt_overlap_sec,
                )
            except ToolError as e:  # no audio stream / range out of bounds / cannot cut or decode (fixed wording)
                skip(disp, e)
                continue
            except Exception as e:  # noqa: BLE001 - whisper / ffmpeg exceptions contain internal paths, so fixed wording and log it
                logger.error(f"[audio_tools] stt failed for {disp}: {e!r}")
                skip(disp, ToolFatal("文字起こしに失敗した", _RETRY))
                continue
            finally:
                if is_tmp:
                    _unlink_quietly(src)

            total, end = info["total_sec"], info["end_sec"]
            if cap > 0 and end is not None:
                budget -= max(0.0, end - info["start_sec"])
            body = (info["text"] or "").strip() or "(無音 / 認識テキスト無し)"
            header = f"{disp} [{_hms(info['start_sec'])}〜{_hms(end)} / 全 {_hms(total)}]"
            if info["overlap_sec"]:
                header += f" ※先頭 {info['overlap_sec']:.0f} 秒は前区間と重複"
            # If there is more left, always state the next start_sec to pass.
            # (Without it a partial transcript may be summarized as if it were the whole)
            tail = ""
            # Do not offer a continuation when end has not advanced past start (to avoid re-calling
            # the same position forever. Decode anomalies are raised by _run_stt)
            if (total is not None and end is not None
                    and end < total - 1 and end > info["start_sec"] + 1):
                tail = (
                    f"\n\n⋮ ここまで {_hms(end)} / 全 {_hms(total)} (未処理 "
                    f"{_hms(total - end)})。{NEXT}続きは "
                    f'transcribe_speech(file_id="{fid}", start_sec={int(end)})'
                )
            elif total is None and end is not None and end > info["start_sec"] + 1:
                tail = (
                    "\n\n⋮ 音声全体の長さが取れず、末尾まで届いたか不明。"
                    f'{NEXT}途中で終わっているようなら transcribe_speech(file_id="{fid}", '
                    f"start_sec={int(end)}) で続きを見る。"
                )

            results.append(self._emit(disp, body, tail, header, __user__,
                                      info["start_sec"]))

        if len(failures) == len(results) and not deferred:
            # No file produced a result = nothing to return
            raise _all_failed(failures)
        if failures:
            mark_error_kind("failure")  # some files were skipped

        if deferred:
            results.append(
                f"⋮ 1 回の上限に達したので未処理: {', '.join(deferred)}。"
                f"{NEXT}必要なら file_id を指定して 1 件ずつ呼ぶ。"
            )

        return "\n\n".join(results)

    def _emit(self, disp, body, tail, header, user, start_sec) -> str:
        """Build the result for one file. An over-long body is offloaded to .txt and only the head is returned.

        A truncated transcript is useless (summaries and quotes both need the full text), so
        the full text is registered in the chat as a file and only the head goes into the context.
        Same discipline as sandbox's huge output — protect the context while keeping a route to the full text.
        The extracted-text path also goes through here (so that not only the whisper path is protected)."""
        limit = self.valves.max_transcript_chars
        if limit <= 0 or len(body) <= limit:
            return f"### {header}\n{body}{tail}"
        try:
            # One file per range (do not make it look like the same name was overwritten)
            part = f"_{int(start_sec)}s" if start_sec else ""
            name = f"{os.path.splitext(disp)[0]}_文字起こし{part}.txt"
            txt_fid = _register_binary_file(
                (user or {})["id"], name,
                body.encode("utf-8"), "text/plain; charset=utf-8",
            )
        except Exception as e:  # noqa: BLE001 - the result (head) can still be returned, so a normal result with a caveat
            # Even if offloading fails, do not pour in the full text (context protection must not invert on failure)
            logger.error(f"[audio_tools] offloading long text failed: {e!r}")
            mark_error_kind("failure")  # the job of keeping the full text failed (the head is returned)
            return (
                f"### {header}\n{body[:limit]}\n\n"
                f"⋮ 全 {len(body):,} 字のうち冒頭 {limit:,} 字のみ (残りはファイルにも残せなかった)。"
                f"{NEXT}必要なら区間を狭めて (duration_sec) 呼び直す。"
                f"{tail}"
            )
        return (
            f"### {header}\n{body[:limit]}\n\n"
            f"⋮ 全 {len(body):,} 字のうち冒頭 {limit:,} 字まで。全文は [{name}](/api/v1/files/{txt_fid}/content) "
            f"にある。{NEXT}残りの中身が要るなら、run_subagent が使えるときは instruction に "
            f"file_id={txt_fid} と知りたいことを書いて読ませる (子が read_attachment で読む)。{tail}"
        )

    @staticmethod
    def _run_stt(
        __request__: Any,
        src: str,
        normalize: bool = True,
        ffmpeg_timeout: int = 120,
        start_sec: float = 0.0,
        duration_sec: Optional[float] = None,
        segment_cap: float = 0.0,
        overlap_sec: float = 0.0,
    ) -> dict:
        """Cut out only the needed range of an audio file (local path) and call transcribe().

        With normalize=True it is normalized to 16kHz mono WAV with ffmpeg before being handed over
        (anything ffmpeg can decode generally passes). If normalization fails, the original is passed as-is.
        However, when a range is given (including the cap from start_sec / segment_cap),
        cutting is a requirement, so a failure raises instead of falling back to the original
        (processing from the start and "meant to continue but got the head again" does more harm).
        No audio stream, range out of bounds, cannot cut, unreadable content -> ToolError
        (the caller treats it as a per-file failure; wording is fixed and contains no internal paths).

        When start_sec > 0, cut from overlap_sec earlier — if whisper's window is cut at the range
        boundary the boundary words break (observed: "血圧" -> "血" / "1月").
        The overlapping text appears in both ranges. The returned start_sec is the logical position (excluding the overlap).

        transcribe is synchronous (ThreadPoolExecutor inside). The temp file we pass and the
        {id}.json sidecar that transcribe writes are cleaned up by us.
        Returns: {"text", "total_sec", "start_sec", "end_sec", "overlap_sec"}."""
        probe = _probe_audio(src, ffmpeg_timeout)
        if not probe:
            raise _not_audio()
        total = probe.get("duration_sec")
        start = max(0.0, start_sec or 0.0)
        if total is not None and start >= total - 0.5:
            raise ToolOutcome(
                f"開始位置 {_hms(start)} は音声長 {_hms(total)} の外で、その位置に音声は無い",
                "位置を確かめ直す。",
            )

        # Cap on the length handled in one call. Anything beyond is not cut but left to the next call
        want = duration_sec if (duration_sec and duration_sec > 0) else None
        want_given = want is not None  # whether the model passed duration_sec (to distinguish from the cap)
        cap = segment_cap if segment_cap and segment_cap > 0 else None
        if cap:
            if want is None:
                if total is None or (total - start) > cap:
                    want = cap
            elif want > cap:
                want = cap

        # Run-up so boundary words are not broken. Not needed for a call from the start
        overlap = min(max(0.0, overlap_sec), start)
        cut_from = start - overlap
        cut_len = (want + overlap) if want is not None else None

        cut = cut_from > 0 or cut_len is not None
        work = None  # working temp file handed to whisper (always tmp; cleanup in finally)
        try:
            if cut or normalize:
                work = _normalize_to_wav(src, ffmpeg_timeout, cut_from, cut_len)
                if not work and cut:
                    if start > 0 or want_given:
                        # The model specified a range = dropping it (start to cap) is likely to pass
                        raise ToolCallError(
                            "指定区間を切り出せなかった (この音声は区間指定で扱えない)",
                            "start_sec / duration_sec を外して呼び直す。",
                        )
                    # Even without a range, audio longer than the per-call cap (or of unknown length) needs cutting.
                    # There is no argument to drop, so re-calling gives the same — a runtime failure
                    raise ToolFailure(
                        "この音声は区切って処理できなかった (1 回で扱える長さより長いか、長さが分からない)",
                        "呼び直さず、この事実をユーザーに伝える。",
                    )
            if not work:
                # Copy even when passing the original — transcribe writes {id}.json next to the
                # path it is given, so keep the Storage entity directory clean
                fd, work = tempfile.mkstemp(suffix=os.path.splitext(src)[1] or ".wav")
                os.close(fd)
                shutil.copyfile(src, work)

            # The length actually cut. Used for end detection, remainder calculation and detecting decode anomalies
            got = _probe_audio(work, ffmpeg_timeout).get("duration_sec")
            seg = None if got is None else max(0.0, got - overlap)  # length of the logical range
            if start > 0 and (seg is None or seg < 0.5):
                raise ToolOutcome(f"開始位置 {_hms(start)} 以降に音声が無い (末尾に到達済み)")
            # Extremely short relative to the expected length = header readable but content cannot be
            # decoded (broken file). Reporting it as silence would yield a wrong "it was silent" answer,
            # and the next start_sec would not advance, making the same call repeat
            expect = want if want is not None else (
                None if total is None else total - start)
            if expect is not None and seg is not None and expect > 1.0 \
                    and seg < min(expect * 0.5, expect - 1.0):
                raise ToolFailure(
                    f"音声の中身を読めない — 長さは {_hms(total)} とされているが、"
                    f"実際に読めたのは {seg:.1f} 秒だけ (ファイルが壊れている可能性がある)"
                )

            out = _nucllei_transcribe(__request__, work)

            if seg is not None:
                end = start + seg
                # Shorter than requested = read to EOF. For audio of unknown length this is the full length
                if total is None and (want is None or seg < want - 0.5):
                    total = end
            elif want is not None:
                end = start + want
            else:
                end = total
            if total is not None and end is not None:
                end = min(end, total)
            return {
                "text": (out or {}).get("text", ""),
                "total_sec": total,
                "start_sec": start,
                "end_sec": end,
                "overlap_sec": overlap,
            }
        finally:
            if work:
                # Also remove the sidecar transcribe writes ({basename up to the first '.'}.json)
                sidecar = os.path.join(
                    os.path.dirname(work), os.path.basename(work).split(".")[0] + ".json"
                )
                _unlink_quietly(work)
                _unlink_quietly(sidecar)
