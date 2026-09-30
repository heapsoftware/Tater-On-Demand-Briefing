# verba/on_demand_briefing.py
"""On Demand Briefing verba for Tater.

One verba, many briefing definitions. Each briefing is a configurable record
(sections, time window, prompt, style, delivery) so new briefings can be added
from settings without code changes.

Built-in sections (pluggable providers):
  - time: current local date and time.
  - weather: current real-time conditions via the WeatherAPI integration.
  - news: configurable-topic news via Tater's web search, summarized for speech.
  - camera_activity: UniFi Protect smart-detection events in the window.
  - presence: BLE away window ("while I was gone") from native presence history.

Delivery modes per briefing:
  - response (default): plain spoken text through the normal verba response path.
  - announce: delivered through the announcement path with optional looping
    background audio ducked under the TTS stream (same mechanism AI Task uses;
    audio assets under /api/ai-tasks/background-audio/ work here too).
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from dotenv import load_dotenv

from verba_base import ToolVerba
from verba_result import action_failure, action_success

load_dotenv()

logger = logging.getLogger("on_demand_briefing")
logger.setLevel(logging.INFO)

SETTINGS_CATEGORY = "On Demand Briefing"

DEFAULT_DETECTION_TYPES = ["person", "vehicle", "package", "animal"]

MORNING_PROMPT = (
    "Write a short greeting, share the current weather in one or two sentences, then summarize "
    "notable overnight camera detections with approximate times and camera locations. "
    "If there were no detections, say so plainly. Do not invent events."
)
WELCOME_HOME_PROMPT = (
    "State how long the person was away and when they left and returned, then summarize notable "
    "camera detections from that window with approximate times and camera locations. "
    "If nothing happened, say so plainly. Do not invent events."
)
NEWS_PROMPT = (
    "Summarize the latest news using only the provided search results. "
    "Group related stories, keep each item short, and skip duplicates or minor stories."
)


def _default_briefings() -> List[Dict[str, Any]]:
    return [
        {
            "id": "morning",
            "name": "Morning Briefing",
            "enabled": True,
            "trigger_phrases": [
                "morning briefing",
                "daily briefing",
                "give me my briefing",
                "rundown of the day",
            ],
            "sections": ["time", "weather", "camera_activity"],
            "time_window": {"strategy": "since_time", "start_time": "22:00", "end_time": "now"},
            "prompt": MORNING_PROMPT,
            "style": "brief",
            "empty_message": "Good morning. Nothing notable happened overnight.",
            "identity": "",
            "section_options": {
                "camera_activity": {
                    "cameras": [],
                    "detection_types": DEFAULT_DETECTION_TYPES,
                    "max_events": 20,
                }
            },
            "delivery": {"mode": "response"},
        },
        {
            "id": "welcome_home",
            "name": "Welcome Home Briefing",
            "enabled": True,
            "trigger_phrases": [
                "security briefing while i was gone",
                "what happened while i was out",
                "welcome home briefing",
                "what did i miss",
                "while i was gone",
                "while i was away",
                "while i was out",
            ],
            "sections": ["presence", "camera_activity"],
            "time_window": {"strategy": "last_away_period"},
            "prompt": WELCOME_HOME_PROMPT,
            "style": "brief",
            "empty_message": "Welcome back. Nothing was detected while you were out.",
            "identity": "",
            "section_options": {
                "camera_activity": {
                    "cameras": [],
                    "detection_types": DEFAULT_DETECTION_TYPES,
                    "max_events": 20,
                }
            },
            "delivery": {"mode": "response"},
        },
    ]


def _default_briefings_json() -> str:
    return json.dumps(_default_briefings(), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Pure helpers (kept free of Tater imports so they are easy to test)
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return str(value or "").strip()


def _decode_redis_map(raw: Optional[Dict[Any, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in (raw or {}).items():
        key_text = key.decode("utf-8", "ignore") if isinstance(key, (bytes, bytearray)) else str(key)
        if isinstance(value, (bytes, bytearray)):
            out[key_text] = value.decode("utf-8", "ignore")
        elif value is None:
            out[key_text] = ""
        else:
            out[key_text] = str(value)
    return out


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    raw = _text(value).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


def _to_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(float(value))
    except Exception:
        number = int(default)
    return max(minimum, min(maximum, number))


def _parse_hhmm(value: Any, fallback: Optional[int] = None) -> Optional[int]:
    """Parse 'HH:MM' (24h) into minutes since midnight. None when invalid."""
    raw = _text(value).lower().replace(" ", "")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        return fallback
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return fallback
    return hour * 60 + minute


def _local_now(now_ts: Optional[float] = None) -> datetime:
    if now_ts is not None:
        return datetime.fromtimestamp(float(now_ts)).astimezone()
    return datetime.now().astimezone()


def _minutes_of_day(moment: datetime) -> int:
    return moment.hour * 60 + moment.minute


def resolve_since_time_window(
    config: Dict[str, Any],
    *,
    now: datetime,
) -> Tuple[Optional[datetime], Optional[datetime], str]:
    """Resolve a `since_time` window. Returns (start, end, error)."""
    start_minutes = _parse_hhmm(config.get("start_time"))
    if start_minutes is None:
        return None, None, "since_time window needs a start_time like '22:00'."
    start = now.replace(hour=start_minutes // 60, minute=start_minutes % 60, second=0, microsecond=0)
    if start > now:
        start -= timedelta(days=1)

    end_raw = _text(config.get("end_time")).lower() or "now"
    if end_raw == "now":
        return start, now, ""
    end_minutes = _parse_hhmm(end_raw)
    if end_minutes is None:
        return None, None, f"Invalid end_time '{end_raw}'. Use 'now' or 'HH:MM'."
    end = now.replace(hour=end_minutes // 60, minute=end_minutes % 60, second=0, microsecond=0)
    if end <= start:
        return None, None, "since_time end_time must be after start_time."
    return start, end, ""


def resolve_last_n_hours_window(
    config: Dict[str, Any],
    *,
    now: datetime,
) -> Tuple[Optional[datetime], Optional[datetime], str]:
    hours = _to_int(config.get("hours", 4), 4, 1, 720)
    return now - timedelta(hours=hours), now, ""


def compute_away_window(
    events: List[Dict[str, Any]],
    *,
    now_ts: float,
    device_token: str = "",
    grace_s: float = 600.0,
    min_absence_s: float = 900.0,
    lookback_s: float = 48 * 3600.0,
) -> Dict[str, Any]:
    """Derive the most recent away period from native presence history.

    events: presence history rows (any order) with {"type", "at", "device_id"}.
      Only "left_home" and "arrived_home" rows are used. When device_token is
      empty, absences from all devices are merged (union) into one window.
    grace_s: absences shorter than this are treated as BLE dropouts and ignored.
    min_absence_s: absences shorter than this do not count as a departure.
    lookback_s: only absences starting within this window are considered.

    Returns {"status": "away_window"|"still_away"|"no_departure"|"no_data",
             "start", "end", "duration_s"} with epoch seconds.
    """
    token = _text(device_token).casefold()
    per_device: Dict[str, List[Tuple[float, str]]] = {}
    for row in events or []:
        if not isinstance(row, dict):
            continue
        event_type = _text(row.get("type")).lower()
        if event_type not in {"left_home", "arrived_home"}:
            continue
        row_device = _text(row.get("device_id")).casefold()
        if token and row_device and row_device != token:
            continue
        try:
            at = float(row.get("at") or 0.0)
        except Exception:
            continue
        if at > 0:
            per_device.setdefault(row_device, []).append((at, event_type))

    if not per_device:
        return {"status": "no_data", "start": None, "end": None, "duration_s": None}

    cutoff = now_ts - max(0.0, float(lookback_s))

    intervals: List[Tuple[float, float]] = []
    still_away: List[float] = []
    for rows in per_device.values():
        rows.sort()
        index = 0
        count = len(rows)
        while index < count:
            at, event_type = rows[index]
            if event_type == "arrived_home":
                index += 1
                continue
            # left_home: pair with this device's next arrival, if any.
            end_ts = None
            scan = index + 1
            while scan < count:
                next_at, next_type = rows[scan]
                if next_type == "arrived_home":
                    end_ts = next_at
                    break
                scan += 1
            if end_ts is None:
                still_away.append(at)
                break
            intervals.append((at, end_ts))
            index = scan + 1

    # Drop dropouts shorter than the grace period (BLE flicker).
    if grace_s > 0:
        intervals = [(start, end) for start, end in intervals if (end - start) >= grace_s]

    # Short absences do not count as a departure.
    if min_absence_s > grace_s:
        intervals = [(start, end) for start, end in intervals if (end - start) >= min_absence_s]

    intervals = [(start, end) for start, end in intervals if start >= cutoff]

    # Merge overlapping absences across devices into one household window.
    intervals.sort()
    merged: List[Tuple[float, float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    still_away = [at for at in still_away if at >= cutoff and (now_ts - at) >= min_absence_s]
    if still_away:
        earliest = min(still_away)
        return {
            "status": "still_away",
            "start": earliest,
            "end": now_ts,
            "duration_s": now_ts - earliest,
        }

    if not merged:
        return {"status": "no_departure", "start": None, "end": None, "duration_s": None}

    start, end = merged[-1]
    return {
        "status": "away_window",
        "start": start,
        "end": end,
        "duration_s": end - start,
    }


def resolve_window(
    time_window: Dict[str, Any],
    away: Dict[str, Any],
    *,
    now: datetime,
) -> Tuple[Optional[datetime], Optional[datetime], str]:
    strategy = _text((time_window or {}).get("strategy")).lower() or "since_time"
    if strategy == "last_away_period":
        if away.get("status") in {"away_window", "still_away"}:
            try:
                start = datetime.fromtimestamp(float(away.get("start"))).astimezone()
                end = datetime.fromtimestamp(float(away.get("end"))).astimezone()
                return start, end, ""
            except Exception:
                return None, None, "Presence window could not be converted to times."
        if away.get("status") == "no_data":
            return None, None, "No presence history is available yet for that person."
        return None, None, "No departure was found in the lookback window."
    if strategy == "last_n_hours":
        return resolve_last_n_hours_window(time_window or {}, now=now)
    return resolve_since_time_window(time_window or {}, now=now)


def natural_duration(seconds: Optional[float]) -> str:
    try:
        total = int(round(float(seconds or 0) / 60.0))
    except Exception:
        return "unknown time"
    if total < 1:
        return "less than a minute"
    hours, minutes = divmod(total, 60)
    if hours and minutes:
        return f"{hours} hour{'s' if hours != 1 else ''} and {minutes} minute{'s' if minutes != 1 else ''}"
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def natural_time(moment: Optional[datetime]) -> str:
    if moment is None:
        return "unknown time"
    return moment.strftime("%-I:%M %p").lstrip("0").lower() if hasattr(moment, "strftime") else "unknown time"


def spoken_flatten(text: Any, max_chars: int = 1600) -> str:
    """Make LLM output safe for TTS: strip markdown, lists, and URLs."""
    out = _text(text)
    out = re.sub(r"<[^>]+>", " ", out)
    out = re.sub(r"https?://\S+", " ", out)
    out = re.sub(r"[`*_#>]{1,}", " ", out)
    out = re.sub(r"\[\s*([^\]]+)\s*\]\([^)]*\)", r"\1", out)
    out = re.sub(r"\s*[-•]\s+", " ", out)
    out = re.sub(r"\s+", " ", out).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rstrip() + "..."
    return out


def normalize_briefings(raw: Any) -> Tuple[List[Dict[str, Any]], str]:
    """Validate the BRIEFINGS_JSON payload. Returns (briefings, error)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except Exception as exc:
            return [], f"Briefing definitions are not valid JSON: {exc}"
    if not isinstance(raw, list):
        return [], "Briefing definitions must be a JSON array of briefing objects."

    briefings: List[Dict[str, Any]] = []
    seen_ids = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], f"Briefing #{index + 1} must be an object."
        briefing_id = _text(item.get("id")).lower().replace(" ", "_")
        name = _text(item.get("name"))
        if not briefing_id and not name:
            return [], f"Briefing #{index + 1} needs an id or a name."
        briefing_id = briefing_id or name.lower().replace(" ", "_")
        if briefing_id in seen_ids:
            return [], f"Duplicate briefing id '{briefing_id}'."
        seen_ids.add(briefing_id)
        sections = item.get("sections")
        if not isinstance(sections, list) or not [s for s in sections if _text(s)]:
            return [], f"Briefing '{briefing_id}' needs a non-empty sections list."
        briefings.append({**item, "id": briefing_id, "name": name or briefing_id.replace("_", " ").title()})
    return briefings, ""


def match_briefing(
    briefings: List[Dict[str, Any]],
    *,
    query: str,
    explicit: str = "",
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Pick the briefing for a request. Returns (briefing, error)."""
    enabled = [b for b in briefings if _to_bool(b.get("enabled"), True)]
    if not enabled:
        return None, "No briefings are enabled. Enable one in the On Demand Briefing settings."

    wanted = _text(explicit).casefold()
    if wanted:
        for briefing in enabled:
            if briefing["id"].casefold() == wanted or briefing["name"].casefold() == wanted:
                return briefing, ""
        for briefing in enabled:
            phrases = [briefing["name"], *(briefing.get("trigger_phrases") or [])]
            if any(_text(p).casefold() == wanted for p in phrases):
                return briefing, ""
        return None, f"No briefing matched '{explicit}'."

    hay = " " + re.sub(r"[^a-z0-9\s]", " ", _text(query).casefold()) + " "
    for briefing in enabled:
        phrases = [briefing["name"], *(briefing.get("trigger_phrases") or [])]
        for phrase in phrases:
            token = re.sub(r"[^a-z0-9\s]", " ", _text(phrase).casefold()).strip()
            if token and f" {token} " in hay:
                return briefing, ""

    if len(enabled) == 1:
        return enabled[0], ""
    names = ", ".join(b["name"] for b in enabled[:6])
    return None, f"Which briefing would you like? Available: {names}."


def _identity_from_context(context: Any) -> str:
    ctx = context if isinstance(context, dict) else {}
    origin = ctx.get("origin") if isinstance(ctx.get("origin"), dict) else {}
    for key in ("user_name", "person_name", "speaker_name", "user"):
        value = _text(ctx.get(key) or origin.get(key))
        if value:
            return value
    return ""


def _person_instructions_from_context(context: Any) -> str:
    """Trusted person instructions from Settings > People, attached to the
    portal origin Tater passes in (e.g. 'My name is Steven, but always call
    me sir.'). These tell the briefing how to address the user."""
    ctx = context if isinstance(context, dict) else {}
    sources = []
    if isinstance(ctx.get("origin"), dict):
        sources.append(ctx["origin"])
    sources.append(ctx)
    for source in sources:
        resolution = source.get("people_resolution")
        instructions = _text(source.get("person_instructions"))
        if not instructions and isinstance(resolution, dict):
            instructions = _text(resolution.get("instructions"))
        if instructions:
            return " ".join(instructions.split())[:400]
    return ""


def _requesting_satellite_selector(context: Any) -> str:
    """Selector of the Tater satellite the current request came from.

    Tater attaches trusted portal origin data to tool calls; the same origin
    fields the intercom tool uses identify the asking satellite. Returns ""
    when the request did not come from a known satellite."""
    ctx = context if isinstance(context, dict) else {}
    origin = ctx.get("origin") if isinstance(ctx.get("origin"), dict) else {}
    for source in (origin, ctx):
        if not isinstance(source, dict):
            continue
        selector = _text(source.get("satellite_selector"))
        if not selector:
            device_id = _text(source.get("device_id"))
            if device_id.startswith(("host:", "manual:")):
                selector = device_id
        if not selector:
            selector = _text(source.get("selector"))
        if selector:
            return selector
    return ""


def _clamped_int(value: Any, default: int, minimum: int = 0, maximum: int = 100) -> int:
    return _to_int(value, default, minimum, maximum)


def normalize_audio_scene(raw: Any) -> Dict[str, Any]:
    """Normalize the background-audio scene; same shape as the Broadcast verba."""
    scene = raw if isinstance(raw, dict) else {}
    background = scene.get("background") if isinstance(scene.get("background"), dict) else {}
    ducking = scene.get("ducking") if isinstance(scene.get("ducking"), dict) else {}
    finish = scene.get("finish") if isinstance(scene.get("finish"), dict) else {}

    background_url = _text(
        background.get("url")
        or scene.get("background_url")
        or scene.get("background_audio_url")
    )
    if not background_url:
        return {}
    loop_raw = background.get("loop", scene.get("loop", True))
    if isinstance(loop_raw, bool):
        loop = loop_raw
    else:
        loop = _text(loop_raw).lower() not in {"0", "false", "no", "off", "disabled"}
    return {
        "background": {
            "url": background_url,
            "loop": bool(loop),
            "volume_percent": _clamped_int(background.get("volume_percent", scene.get("background_volume_percent")), 60),
        },
        "ducking": {
            "target_percent": _clamped_int(ducking.get("target_percent", scene.get("ducking_target_percent")), 35),
            "attack_ms": _clamped_int(ducking.get("attack_ms", scene.get("ducking_attack_ms")), 150, 0, 10000),
            "release_ms": _clamped_int(ducking.get("release_ms", scene.get("ducking_release_ms")), 350, 0, 10000),
        },
        "finish": {
            "fade_ms": _clamped_int(finish.get("fade_ms", scene.get("fade_ms")), 500, 0, 10000),
        },
    }


# ---------------------------------------------------------------------------
# Background audio uploads (same Agent Lab store as the AI Task core)
# ---------------------------------------------------------------------------

BACKGROUND_AUDIO_MAX_UPLOAD_BYTES = 16 * 1024 * 1024

_BACKGROUND_AUDIO_UPLOAD_EXTENSIONS = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
}


def _background_audio_uploads_dir() -> Optional[Path]:
    """Directory shared with the AI Task core for uploaded background audio."""
    try:
        from tater_paths import agent_lab_path

        return agent_lab_path("ai_task", "background_audio", "uploads").resolve()
    except Exception:
        configured = str(os.getenv("TATER_AGENT_ROOT") or "").strip()
        base = Path(configured).expanduser() if configured else Path.cwd() / "agent_lab"
        try:
            return (base / "ai_task" / "background_audio" / "uploads").resolve()
        except Exception:
            return None


def _background_audio_base_url() -> str:
    try:
        port = int(str(os.getenv("HTMLUI_PORT") or "8501").strip())
    except Exception:
        port = 8501
    if port < 1 or port > 65535:
        port = 8501
    return f"http://127.0.0.1:{port}/api/ai-tasks/background-audio"


def _store_background_audio_upload(raw: Any) -> str:
    """Persist an uploaded audio file into Agent Lab; returns its asset URL.

    Accepts the {filename, content_type, data_b64} payload the web UI file
    field emits. Raises ValueError with a user-friendly message on bad input
    so the settings save fails visibly."""
    upload = raw
    if isinstance(upload, str):
        upload_text = upload.strip()
        if not upload_text:
            raise ValueError("Choose a WAV, MP3, or FLAC file to upload.")
        try:
            upload = json.loads(upload_text)
        except Exception as exc:
            raise ValueError("The uploaded background audio could not be decoded.") from exc
    upload = upload if isinstance(upload, dict) else {}
    encoded = _text(upload.get("data_b64"))
    if not encoded:
        raise ValueError("Choose a WAV, MP3, or FLAC file to upload.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("The uploaded background audio could not be decoded.") from exc
    if not data:
        raise ValueError("The uploaded background audio is empty.")
    if len(data) > BACKGROUND_AUDIO_MAX_UPLOAD_BYTES:
        raise ValueError("Uploaded background audio must be 16 MB or smaller.")

    content_type = _text(upload.get("content_type")).lower()
    extension = ""
    source_name = _text(upload.get("filename"))
    source_suffix = Path(source_name).suffix.lower() if source_name else ""
    if source_suffix in {".wav", ".mp3", ".flac"}:
        extension = source_suffix
    elif content_type in _BACKGROUND_AUDIO_UPLOAD_EXTENSIONS:
        extension = _BACKGROUND_AUDIO_UPLOAD_EXTENSIONS[content_type]
    else:
        detected = _detect_background_audio_extension(data)
        extension = detected
    if not extension:
        raise ValueError("Uploaded background audio must be a WAV, MP3, or FLAC file.")

    safe_stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(source_name).stem if source_name else "background-audio").strip("-_").lower()
    if not safe_stem:
        safe_stem = "background-audio"
    filename = f"{safe_stem[:48]}-{hashlib.sha256(data).hexdigest()[:12]}{extension}"

    root = _background_audio_uploads_dir()
    if root is None:
        raise ValueError("The Agent Lab directory is unavailable, so the upload could not be stored.")
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    if not path.is_file() or path.stat().st_size != len(data):
        temp_path = root / f".{filename}.{uuid.uuid4().hex}.tmp"
        try:
            temp_path.write_bytes(data)
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
    return f"{_background_audio_base_url()}/uploads/{quote(filename)}"


def _detect_background_audio_extension(data: bytes) -> str:
    """Sniff WAV/MP3/FLAC from magic bytes when the name/content-type lie."""
    magic = data[:16]
    if magic[:4] == b"RIFF" and magic[8:12] == b"WAVE":
        return ".wav"
    if magic[:3] == b"ID3" or magic[:2] == b"\xff\x3b" or (len(magic) >= 2 and magic[0] == 0xFF and (magic[1] & 0xE0) == 0xE0):
        return ".mp3"
    if magic[:4] == b"fLaC":
        return ".flac"
    return ""


# ---------------------------------------------------------------------------
# Web UI briefing form (settings-field hook helpers)
# ---------------------------------------------------------------------------

SECTION_IDS = ["time", "weather", "news", "presence", "camera_activity"]

WINDOW_STRATEGIES = [
    {"value": "since_time", "label": "Since a time"},
    {"value": "last_away_period", "label": "Last away period"},
    {"value": "last_n_hours", "label": "Last N hours"},
]

DELIVERY_MODES = [
    {"value": "response", "label": "Reply in conversation"},
    {"value": "announce", "label": "Announce on satellites"},
]

NEW_BRIEF_TOKEN = "NEW"
BRIEF_FORM_PREFIX = "BRIEF_"


def _form_prefix(id_token: str) -> str:
    return f"{BRIEF_FORM_PREFIX}{id_token}__"


def _split_list(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [_text(v) for v in raw if _text(v)]
    raw = _text(raw)
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [_text(v) for v in parsed if _text(v)]
        except Exception:
            pass
    return [part.strip() for part in raw.split(",") if part.strip()]


def _field(key: str, **meta: Any) -> Dict[str, Any]:
    return {"key": key, **meta}


def briefing_form_fields(briefings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build web UI settings fields: one editable group per briefing plus an
    "Add a briefing" group. Rendered by Tater's generic ManifestField form
    (supports section headers, show_when conditions, multiselect, etc.)."""
    fields: List[Dict[str, Any]] = []
    for item in briefings:
        fields.extend(_briefing_group_fields(item, _form_prefix(item["id"]), include_remove=True))
    fields.extend(_briefing_group_fields({}, _form_prefix(NEW_BRIEF_TOKEN), include_remove=False))
    return fields


def _briefing_group_fields(item: Dict[str, Any], prefix: str, *, include_remove: bool) -> List[Dict[str, Any]]:
    is_new = prefix == _form_prefix(NEW_BRIEF_TOKEN)
    enabled = _to_bool(item.get("enabled"), True) if not is_new else True
    tw = item.get("time_window") if isinstance(item.get("time_window"), dict) else {}
    delivery = item.get("delivery") if isinstance(item.get("delivery"), dict) else {}
    cam = (item.get("section_options") or {}).get("camera_activity") if isinstance(item.get("section_options"), dict) else {}
    cam = cam if isinstance(cam, dict) else {}
    news = (item.get("section_options") or {}).get("news") if isinstance(item.get("section_options"), dict) else {}
    news = news if isinstance(news, dict) else {}
    weather = (item.get("section_options") or {}).get("weather") if isinstance(item.get("section_options"), dict) else {}
    weather = weather if isinstance(weather, dict) else {}
    sections = [s for s in (item.get("sections") or []) if isinstance(s, str)]
    window = _text(tw.get("strategy")) or ("since_time" if not is_new else "since_time")
    mode = _text(delivery.get("mode")).lower() or "response"
    title = str(item.get("name") or "").strip() or ("New briefing" if is_new else "?")

    if is_new:
        header = _field(
            f"{prefix}HDR",
            type="section",
            label="Add a briefing",
            description="Give it a name to create a new briefing when settings are saved. "
            "Leave the name empty to skip. Saved briefings get their own group the next time you open settings.",
        )
    else:
        header = _field(
            f"{prefix}HDR",
            type="section",
            label=f"Briefing: {title}",
            description=f"Id: {item.get('id')}. Uncheck Enabled to keep it configured but inactive.",
        )

    group = [header]
    if include_remove:
        group.append(
            _field(
                f"{prefix}REMOVE",
                label="Remove this briefing",
                type="checkbox",
                value=False,
                description="Deleting every briefing resets On Demand Briefing to its built-in Morning and Welcome Home briefings.",
            )
        )
        group.append(
            _field(
                f"{prefix}ENABLED",
                label="Enabled",
                type="checkbox",
                value=enabled,
                description="Disabled briefings are kept but never matched by voice requests.",
            )
        )
    group.extend(
        [
            _field(
                f"{prefix}NAME",
                label="Name",
                type="text",
                value=item.get("name") or "",
                placeholder="Evening Headlines" if is_new else "",
                description="Spoken name; also used as a trigger phrase.",
            ),
            _field(
                f"{prefix}PHRASES",
                label="Trigger phrases (comma-separated)",
                type="text",
                value=", ".join(_split_list(item.get("trigger_phrases"))),
                placeholder="evening news, daily recap",
            ),
            _field(
                f"{prefix}SECTIONS",
                label="Sections (order matters)",
                type="multiselect",
                options=SECTION_IDS,
                value=sections if not is_new else [],
                description="Sections run in checkbox order: time, weather, news, presence, camera_activity.",
            ),
            _field(
                f"{prefix}WINDOW",
                label="Time window",
                type="select",
                options=WINDOW_STRATEGIES,
                value=window,
                description='"Last away period" uses presence history (presence section covers the away window).',
            ),
            _field(
                f"{prefix}START",
                label="Window start (HH:MM)",
                type="text",
                value=_text(tw.get("start_time")) or "22:00",
                show_when={"key": f"{prefix}WINDOW", "values": ["since_time"]},
            ),
            _field(
                f"{prefix}END",
                label='Window end ("now" or HH:MM)',
                type="text",
                value=_text(tw.get("end_time")) or "now",
                show_when={"key": f"{prefix}WINDOW", "values": ["since_time"]},
            ),
            _field(
                f"{prefix}HOURS",
                label="Window hours",
                type="number",
                value=int(_to_int(tw.get("hours"), 4, 1, 720)),
                show_when={"key": f"{prefix}WINDOW", "values": ["last_n_hours"]},
            ),
            _field(
                f"{prefix}PROMPT",
                label="Writing prompt",
                type="textarea",
                rows=4,
                value=_text(item.get("prompt")) if not is_new else "",
                placeholder="Summarize the sections for the user, then add one personal tip.",
            ),
            _field(
                f"{prefix}STYLE",
                label="TTS style",
                type="select",
                options=["brief", "detailed"],
                value=_text(item.get("style")).lower() or "brief",
            ),
            _field(
                f"{prefix}EMPTY",
                label="Empty-briefing message",
                type="text",
                value=_text(item.get("empty_message")),
                placeholder="Spoken when a section has nothing to report",
            ),
            _field(
                f"{prefix}IDENTITY",
                label="Presence identity",
                type="text",
                value=_text(item.get("identity")),
                description="Tracked device id or name used to resolve the away window. Empty uses the person asking.",
            ),
            _field(
                f"{prefix}WEATHER_LOCATION",
                label="Weather location",
                type="text",
                value=_text(weather.get("location")),
                placeholder="City, Region",
                description="Used when the section list includes weather. Empty uses the default location setting.",
            ),
            _field(
                f"{prefix}WEATHER_UNITS",
                label="Weather units",
                type="select",
                options=["default", "us", "metric"],
                value=_text(weather.get("units")).lower() or "default",
                description="Empty (default) uses the default units setting.",
            ),
            _field(
                f"{prefix}WEATHER_FORECAST",
                label="Include next-day forecast",
                type="checkbox",
                value=_to_bool(weather.get("include_forecast"), False),
            ),
            _field(
                f"{prefix}NEWS_TOPIC",
                label="News topic",
                type="text",
                value=_text(news.get("topic") or news.get("query")),
                placeholder="technology",
                description="Used when the section list includes news. Search appends \" news\" unless the topic already contains it.",
            ),
            _field(
                f"{prefix}NEWS_MAX_ITEMS",
                label="News max headlines",
                type="number",
                value=int(_to_int(news.get("max_items"), 5, 1, 10)),
            ),
            _field(
                f"{prefix}NEWS_SENTENCES",
                label="News sentences per headline",
                type="number",
                value=int(_to_int(news.get("max_sentences"), 2, 1, 5)),
            ),
            _field(
                f"{prefix}CAMERAS",
                label="Camera filter (comma-separated)",
                type="text",
                value=", ".join(_split_list(cam.get("cameras"))),
                description="Only these UniFi Protect camera names are included. Empty means all cameras.",
            ),
            _field(
                f"{prefix}DETECTIONS",
                label="Camera detection types",
                type="multiselect",
                options=["person", "vehicle", "package", "animal", "motion"],
                description="Include 'motion' to also report plain motion events that have no person/vehicle/package/animal tag.",
                value=_split_list(cam.get("detection_types")) or (DEFAULT_DETECTION_TYPES if not is_new else []),
            ),
            _field(
                f"{prefix}MAX_EVENTS",
                label="Max camera events",
                type="number",
                value=int(_to_int(cam.get("max_events"), 20, 1, 200)),
            ),
            _field(
                f"{prefix}DELIVERY",
                label="Delivery",
                type="select",
                options=DELIVERY_MODES,
                value=mode,
                description='"Announce" sends the finished briefing to Tater satellites.',
            ),
            _field(
                f"{prefix}TARGETS",
                label="Announce targets (comma-separated)",
                type="text",
                value=", ".join(_split_list(delivery.get("targets"))),
                show_when={"key": f"{prefix}DELIVERY", "values": ["announce"]},
                description='Satellite targets like "voice_core:kitchen". Leave empty and tick "Announce on the asking satellite" to play only where you asked, or leave both empty for every connected satellite.',
            ),
            _field(
                f"{prefix}ASK_SATELLITE",
                label="Announce on the asking satellite",
                type="checkbox",
                value=_to_bool(delivery.get("requester_target"), False),
                show_when={"key": f"{prefix}DELIVERY", "values": ["announce"]},
                description="Play the briefing only on the satellite you asked from. Applies when the targets field above is empty; if the asking device cannot be determined, every connected satellite is used.",
            ),
            _field(
                f"{prefix}BACKGROUND_URL",
                label="Background audio URL",
                type="text",
                value=_text(((delivery.get("background_audio") or {}).get("background") or {}).get("url") or (delivery.get("background_audio") or {}).get("background_url")) if isinstance(delivery.get("background_audio"), dict) else "",
                show_when={"key": f"{prefix}DELIVERY", "values": ["announce"]},
                placeholder="/api/ai-tasks/background-audio/presets/news.wav",
                description="Looping audio ducked under the announcement TTS. Any asset under /api/ai-tasks/background-audio/. An upload below overrides this. Empty means no background audio.",
            ),
            _field(
                f"{prefix}BACKGROUND_UPLOAD",
                label="Upload background audio",
                type="file",
                accept=".wav,.mp3,.flac,audio/wav,audio/mpeg,audio/flac",
                file_encoding="base64",
                max_bytes=BACKGROUND_AUDIO_MAX_UPLOAD_BYTES,
                description="WAV, MP3, or FLAC up to 16 MB. Stored in the shared AI Task Agent Lab folder when you save and used as the background audio.",
                value="",
                show_when={"key": f"{prefix}DELIVERY", "values": ["announce"]},
            ),
            _field(
                f"{prefix}BACKGROUND_LOOP",
                label="Loop background audio",
                type="checkbox",
                value=_to_bool((((delivery.get("background_audio") or {}).get("background") or {}).get("loop")), True),
                show_when={"key": f"{prefix}DELIVERY", "values": ["announce"]},
            ),
            _field(
                f"{prefix}BACKGROUND_VOLUME",
                label="Background volume (percent)",
                type="number",
                value=int(_to_int(((delivery.get("background_audio") or {}).get("background") or {}).get("volume_percent"), 60, 0, 100)),
                show_when={"key": f"{prefix}DELIVERY", "values": ["announce"]},
            ),
        ]
    )
    return group


def _apply_briefing_form(item: Dict[str, Any], values: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    """Overlay editable form values onto an existing briefing definition,
    preserving keys the form does not expose."""
    out = dict(item)
    tw = dict(item.get("time_window") if isinstance(item.get("time_window"), dict) else {})

    def val(suffix: str) -> Any:
        return values.get(f"{prefix}{suffix}")

    name = _text(val("NAME"))
    if name:
        out["name"] = name
    if f"{prefix}ENABLED" in values:
        out["enabled"] = _to_bool(val("ENABLED"), _to_bool(out.get("enabled"), True))

    phrases = _split_list(val("PHRASES"))
    if phrases:
        out["trigger_phrases"] = phrases

    sections = _split_list(val("SECTIONS"))
    if sections:
        out["sections"] = [s for s in SECTION_IDS if s in sections] + [s for s in sections if s not in SECTION_IDS]

    strategy = _text(val("WINDOW"))
    if strategy in {s["value"] for s in WINDOW_STRATEGIES}:
        if strategy == "since_time":
            tw = {
                "strategy": strategy,
                "start_time": _text(val("START")) or _text(tw.get("start_time")) or "22:00",
                "end_time": _text(val("END")) or "now",
            }
        elif strategy == "last_n_hours":
            tw = {"strategy": strategy, "hours": _to_int(val("HOURS"), _to_int(tw.get("hours"), 4, 1, 720), 1, 720)}
        else:
            tw = {"strategy": strategy}
        out["time_window"] = tw

    if val("PROMPT") is not None:
        out["prompt"] = _text(val("PROMPT"))
    style = _text(val("STYLE")).lower()
    if style in {"brief", "detailed"}:
        out["style"] = style
    if val("EMPTY") is not None:
        out["empty_message"] = _text(val("EMPTY"))
    if val("IDENTITY") is not None:
        out["identity"] = _text(val("IDENTITY"))

    cameras = _split_list(val("CAMERAS"))
    detections = _split_list(val("DETECTIONS"))
    news_topic = _text(val("NEWS_TOPIC"))
    weather_location = _text(val("WEATHER_LOCATION"))
    weather_units = _text(val("WEATHER_UNITS"))
    if (
        cameras
        or detections
        or news_topic
        or weather_location
        or f"{prefix}MAX_EVENTS" in values
        or f"{prefix}NEWS_MAX_ITEMS" in values
        or f"{prefix}WEATHER_FORECAST" in values
    ):
        section_options = dict(out.get("section_options") or {})
        if cameras or detections or f"{prefix}MAX_EVENTS" in values:
            options = dict(section_options.get("camera_activity") or {})
            if cameras:
                options["cameras"] = cameras
            if detections:
                options["detection_types"] = detections
            if f"{prefix}MAX_EVENTS" in values:
                options["max_events"] = _to_int(val("MAX_EVENTS"), int(options.get("max_events") or 20), 1, 200)
            section_options["camera_activity"] = options
        if news_topic or f"{prefix}NEWS_MAX_ITEMS" in values or f"{prefix}NEWS_SENTENCES" in values:
            news = dict(section_options.get("news") or {})
            if news_topic:
                news["topic"] = news_topic
            if f"{prefix}NEWS_MAX_ITEMS" in values:
                news["max_items"] = _to_int(val("NEWS_MAX_ITEMS"), int(news.get("max_items") or 5), 1, 10)
            if f"{prefix}NEWS_SENTENCES" in values:
                news["max_sentences"] = _to_int(val("NEWS_SENTENCES"), int(news.get("max_sentences") or 2), 1, 5)
            section_options["news"] = news
        if weather_location or weather_units or f"{prefix}WEATHER_FORECAST" in values:
            weather_opts = dict(section_options.get("weather") or {})
            if weather_location:
                weather_opts["location"] = weather_location
            if weather_units in {"us", "metric"}:
                weather_opts["units"] = weather_units
            if f"{prefix}WEATHER_FORECAST" in values:
                weather_opts["include_forecast"] = _to_bool(val("WEATHER_FORECAST"))
            section_options["weather"] = weather_opts
        out["section_options"] = section_options

    mode = _text(val("DELIVERY")).lower()
    if mode in {"response", "announce"}:
        delivery = dict(item.get("delivery") or {}) if isinstance(item.get("delivery"), dict) else {}
        delivery["mode"] = mode
        if mode == "announce":
            targets = _split_list(val("TARGETS"))
            if targets:
                delivery["targets"] = targets
            if f"{prefix}ASK_SATELLITE" in values:
                delivery["requester_target"] = _to_bool(val("ASK_SATELLITE"), False)
            upload = values.get(f"{prefix}BACKGROUND_UPLOAD") if f"{prefix}BACKGROUND_UPLOAD" in values else None
            upload_url = _store_background_audio_upload(upload) if upload not in (None, "") else ""
            if f"{prefix}BACKGROUND_URL" in values or upload_url:
                background_url = upload_url or _text(val("BACKGROUND_URL"))
                existing = delivery.get("background_audio") if isinstance(delivery.get("background_audio"), dict) else {}
                if background_url:
                    background = dict(existing.get("background") or {})
                    background["url"] = background_url
                    if f"{prefix}BACKGROUND_LOOP" in values:
                        background["loop"] = _to_bool(val("BACKGROUND_LOOP"), _to_bool(background.get("loop"), True))
                    if f"{prefix}BACKGROUND_VOLUME" in values:
                        background["volume_percent"] = _to_int(
                            val("BACKGROUND_VOLUME"), _to_int(background.get("volume_percent"), 60, 0, 100), 0, 100
                        )
                    delivery["background_audio"] = {**existing, "background": background}
                else:
                    delivery.pop("background_audio", None)
        out["delivery"] = delivery
    return out


def _new_briefing_from_form(values: Dict[str, Any], existing_ids: List[str]) -> Dict[str, Any]:
    prefix = _form_prefix(NEW_BRIEF_TOKEN)
    name = _text(values.get(f"{prefix}NAME"))
    briefing_id = name.lower().replace(" ", "_")
    candidate, counter = briefing_id, 2
    while candidate in existing_ids:
        candidate = f"{briefing_id}_{counter}"
        counter += 1
    sections = _split_list(values.get(f"{prefix}SECTIONS"))
    item = _apply_briefing_form({"id": candidate, "name": name}, values, prefix)
    item.setdefault("sections", ["time"])
    return item


def _drop_form_values(values: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in values.items() if not k.startswith(f"{BRIEF_FORM_PREFIX}")}


# ---------------------------------------------------------------------------
# Section providers
# ---------------------------------------------------------------------------

SECTION_PROVIDERS: Dict[str, Any] = {}


def section_provider(name: str):
    """Register a section provider under a stable name."""

    def decorator(func):
        SECTION_PROVIDERS[name] = func
        return func

    return decorator


def _weather_api_module():
    try:
        from tateros import integration_store as integration_store_module

        return integration_store_module.integration_module("weather_api")
    except Exception:
        return None


def _weather_settings(module: Any) -> Dict[str, Any]:
    defaults = {
        "WEATHERAPI_KEY": "",
        "DEFAULT_LOCATION": "",
        "DEFAULT_UNITS": "us",
    }
    try:
        settings = module.read_weatherapi_settings()
        if isinstance(settings, dict):
            defaults.update({k: v for k, v in settings.items() if k in defaults})
    except Exception:
        pass
    return defaults


@section_provider("weather")
async def weather_section(briefing: Dict[str, Any], window: Dict[str, Any], runtime: Dict[str, Any]) -> Dict[str, Any]:
    options = (briefing.get("section_options") or {}).get("weather") or {}
    module = _weather_api_module()
    if module is None:
        return {"ok": False, "summary": "", "data": {}, "error": "The WeatherAPI integration is not enabled."}
    settings = _weather_settings(module)
    if not _text(settings.get("WEATHERAPI_KEY")):
        return {
            "ok": False,
            "summary": "",
            "data": {},
            "error": "Weather is not configured. Set the WeatherAPI key in Settings, Integrations, WeatherAPI.",
        }
    location = _text(options.get("location")) or _text(settings.get("DEFAULT_LOCATION"))
    units = _text(options.get("units")) or _text(settings.get("DEFAULT_UNITS")) or "us"
    include_forecast = _to_bool(options.get("include_forecast"), False)

    def _fetch():
        return module.fetch_weatherapi_forecast(
            location=location,
            days=2 if include_forecast else 1,
            include_aqi=False,
            include_pollen=False,
            include_alerts=False,
            timeout_seconds=12,
        )

    try:
        data, error = await asyncio.to_thread(_fetch)
    except Exception as exc:
        return {"ok": False, "summary": "", "data": {}, "error": f"Weather lookup failed: {exc}"}
    if error or not isinstance(data, dict):
        return {"ok": False, "summary": "", "data": {}, "error": _text(error) or "No weather data returned."}

    current = data.get("current") or {}
    location_info = data.get("location") or {}
    condition = _text((current.get("condition") or {}).get("text")) or "unknown"
    if units == "metric":
        temp = current.get("temp_c")
        feels = current.get("feelslike_c")
        wind = current.get("wind_kph")
        wind_unit = "kilometers per hour"
    else:
        temp = current.get("temp_f")
        feels = current.get("feelslike_f")
        wind = current.get("wind_mph")
        wind_unit = "miles per hour"

    parts = []
    place = _text(location_info.get("name"))
    lead = f"In {place}" if place else "Currently"
    parts.append(f"{lead} it is {condition} at {_text(temp) or 'unknown'} degrees, feels like {_text(feels) or 'unknown'}.")
    if wind is not None:
        parts.append(f"Wind around {_text(wind)} {wind_unit}.")
    if current.get("humidity") is not None:
        parts.append(f"Humidity {_text(current.get('humidity'))} percent.")

    if include_forecast:
        forecast_days = (data.get("forecast") or {}).get("forecastday") or []
        today = next((d for d in forecast_days if _text(d.get("date")) == now_date_str()), None)
        if isinstance(today, dict):
            day = today.get("day") or {}
            if units == "metric":
                high, low = day.get("maxtemp_c"), day.get("mintemp_c")
            else:
                high, low = day.get("maxtemp_f"), day.get("mintemp_f")
            if high is not None and low is not None:
                parts.append(f"Today's high is around {_text(high)} and the low around {_text(low)} degrees.")

    return {
        "ok": True,
        "summary": " ".join(parts),
        "data": {"location": place, "current": {k: current.get(k) for k in ("temp_f", "temp_c", "humidity", "wind_mph", "wind_kph") if current.get(k) is not None}},
        "error": "",
    }


def now_date_str() -> str:
    return _local_now().strftime("%Y-%m-%d")


@section_provider("time")
async def time_section(briefing: Dict[str, Any], window: Dict[str, Any], runtime: Dict[str, Any]) -> Dict[str, Any]:
    now = _local_now()
    hour = now.hour % 12 or 12
    spoken = f"{hour}:{now.minute:02d} {'am' if now.hour < 12 else 'pm'}"
    summary = f"It is {spoken} on {now.strftime('%A, %B ') + str(now.day)}."
    return {"ok": True, "summary": summary, "data": {"iso": now.isoformat()}, "error": ""}


def _search_web_sync(query: str, num_results: int):
    from kernel_tools import search_web

    return search_web(query, num_results=num_results, timeout_sec=15)


@section_provider("news")
async def news_section(briefing: Dict[str, Any], window: Dict[str, Any], runtime: Dict[str, Any]) -> Dict[str, Any]:
    options = (briefing.get("section_options") or {}).get("news") or {}
    topic = _text(options.get("topic") or options.get("query"))
    max_items = _to_int(options.get("max_items", 5), 5, 1, 10)
    max_sentences = _to_int(options.get("max_sentences", 2), 2, 1, 5)
    if not topic:
        return {
            "ok": False,
            "summary": "",
            "data": {},
            "error": "The news section needs a topic. Set it in the briefing's news section options.",
        }

    query = topic if "news" in topic.casefold() else f"{topic} news"
    try:
        result = await asyncio.to_thread(_search_web_sync, query, max_items)
    except Exception as exc:
        return {"ok": False, "summary": "", "data": {}, "error": f"News search failed: {exc}"}
    if not isinstance(result, dict) or not result.get("ok"):
        error = _text((result or {}).get("error")) if isinstance(result, dict) else ""
        return {"ok": False, "summary": "", "data": {}, "error": error or "News search failed."}

    rows = [row for row in (result.get("results") or []) if isinstance(row, dict)]
    rows = rows[:max_items]
    if not rows:
        return {"ok": True, "summary": "", "data": {"items": []}, "error": ""}

    compact = [
        {"title": _text(row.get("title"))[:160], "snippet": _text(row.get("snippet"))[:280]}
        for row in rows
    ]
    llm_client = runtime.get("llm_client")
    system = (
        "You write the news portion of a spoken briefing. Using ONLY the provided search results, "
        f"write at most {max_items} short news items. Each item is at most {max_sentences} sentence(s). "
        "No markdown, no lists, no URLs, no source names. Merge duplicate stories. "
        "If the results are thin, summarize what is there. Output plain sentences only."
    )
    user_payload = json.dumps({"topic": topic, "results": compact}, ensure_ascii=False)
    summary = ""
    try:
        if llm_client is not None:
            response = await llm_client.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_payload},
                ],
                temperature=0.2,
                max_tokens=120 * max_items,
            )
            summary = spoken_flatten((response.get("message") or {}).get("content"), max_chars=1200)
    except Exception as exc:
        logger.warning("[on_demand_briefing] news LLM summary failed: %s", exc)
        summary = ""

    if not summary:
        # Deterministic fallback: first sentence of each snippet.
        bits = []
        for item in compact:
            first = re.split(r"(?<=[.!?])\s+", item["snippet"] or item["title"])[0].strip()
            if first:
                bits.append(first)
        summary = " ".join(bits[:max_items])[:1200]

    return {"ok": True, "summary": summary, "data": {"items": compact}, "error": ""}


def _native_ble():
    try:
        from tater_voice import native_ble

        return native_ble
    except Exception:
        return None


@section_provider("presence")
async def presence_section(briefing: Dict[str, Any], window: Dict[str, Any], runtime: Dict[str, Any]) -> Dict[str, Any]:
    away = runtime.get("away_window") or {}
    status = away.get("status")
    if status == "no_data":
        return {
            "ok": False,
            "summary": "",
            "data": {},
            "error": "I could not find presence data for that person yet. Presence history only exists from when tracking started.",
        }
    if status == "no_departure":
        return {"ok": True, "summary": "", "data": {"away_window": away}, "error": ""}
    if status not in {"away_window", "still_away"}:
        return {"ok": False, "summary": "", "data": {"away_window": away}, "error": "Presence data was unavailable."}

    start = away.get("start")
    end = away.get("end")
    try:
        start_dt = datetime.fromtimestamp(float(start)).astimezone()
        end_dt = datetime.fromtimestamp(float(end)).astimezone()
    except Exception:
        return {"ok": False, "summary": "", "data": {"away_window": away}, "error": "Presence timestamps were invalid."}

    duration = natural_duration(away.get("duration_s"))
    if status == "still_away":
        summary = (
            f"You left around {natural_time(start_dt)} and it has been about {duration} since then."
        )
    else:
        summary = (
            f"You were away from about {natural_time(start_dt)} to {natural_time(end_dt)}, "
            f"roughly {duration}."
        )
    return {
        "ok": True,
        "summary": summary,
        "data": {
            "away_window": {
                "status": status,
                "start_iso": start_dt.isoformat(),
                "end_iso": end_dt.isoformat(),
                "duration_s": away.get("duration_s"),
            }
        },
        "error": "",
    }


def _protect_client():
    try:
        from tateros import integration_store as integration_store_module

        module = integration_store_module.integration_module("unifi_protect")
        if module is None:
            return None
        return module.ProtectClient()
    except Exception as exc:
        logger.warning("[on_demand_briefing] UniFi Protect client unavailable: %s", exc)
        return None


def _first_value(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _protect_event_rows(client: Any, start_ms: int, end_ms: int, limit: int) -> Tuple[List[Dict[str, Any]], str]:
    """Best-effort fetch of Protect events for the window.

    Some Protect consoles do not expose the events endpoint; the caller treats
    any failure as "section unavailable" and keeps the rest of the briefing.
    """
    variants = [
        ("/proxy/protect/integration/v1/events", {"start": start_ms, "end": end_ms, "limit": limit}),
        ("/proxy/protect/integration/v1/events", {"startTimestamp": start_ms, "endTimestamp": end_ms, "limit": limit}),
    ]
    last_error = ""
    for path, params in variants:
        try:
            payload = client._req("GET", path, params=params)
        except Exception as exc:
            last_error = str(exc)
            continue
        rows: List[Dict[str, Any]] = []
        if isinstance(payload, list):
            rows = [row for row in payload if isinstance(row, dict)]
        elif isinstance(payload, dict):
            for key in ("events", "data", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    rows = [row for row in value if isinstance(row, dict)]
                    break
        if rows or not last_error:
            return rows, ""
    return [], last_error or "No events returned."


def _ms_to_dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        raw = float(value)
        if raw > 1_000_000_000_000:  # milliseconds
            raw /= 1000.0
        elif raw < 1_000_000_000:  # relative seconds; ignore
            return None
        return datetime.fromtimestamp(raw).astimezone()
    except Exception:
        return None


@section_provider("camera_activity")
async def camera_activity_section(briefing: Dict[str, Any], window: Dict[str, Any], runtime: Dict[str, Any]) -> Dict[str, Any]:
    start = window.get("start")
    end = window.get("end")
    if start is None or end is None:
        return {"ok": False, "summary": "", "data": {}, "error": "The briefing window could not be resolved."}

    settings_map = runtime.get("settings") or {}
    options = (briefing.get("section_options") or {}).get("camera_activity") or {}
    detection_types = [
        _text(item).lower()
        for item in (options.get("detection_types") or _text(settings_map.get("DEFAULT_DETECTION_TYPES")).split(",") or DEFAULT_DETECTION_TYPES)
        if _text(item)
    ] or DEFAULT_DETECTION_TYPES
    max_events = _to_int(options.get("max_events", 20), 20, 1, 100)
    camera_filter = {_text(c).casefold() for c in (options.get("cameras") or []) if _text(c)}

    client = runtime.get("protect_client")
    if client is None:
        return {
            "ok": False,
            "summary": "",
            "data": {},
            "error": "UniFi Protect is not configured. Set it in Settings, Integrations, UniFi Protect.",
        }

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    try:
        rows, error = await asyncio.to_thread(_protect_event_rows, client, start_ms, end_ms, max(50, max_events * 4))
        cameras = await asyncio.to_thread(client.list_cameras)
    except Exception as exc:
        return {"ok": False, "summary": "", "data": {}, "error": f"UniFi Protect request failed: {exc}"}

    camera_names: Dict[str, str] = {}
    for camera in cameras or []:
        if not isinstance(camera, dict):
            continue
        camera_id = _text(_first_value(camera, "id", "_id", "uuid"))
        name = _text(_first_value(camera, "name", "displayName", "friendlyName")) or camera_id
        if camera_id:
            camera_names[camera_id] = name

    events: List[Dict[str, Any]] = []
    skipped_motion_only = 0
    for row in rows:
        event_type = _text(_first_value(row, "type", "event_type", "smartDetectType")).lower()
        detected = row.get("smartDetectionTypes") or row.get("detectionTypes") or row.get("smartTypes") or []
        if isinstance(detected, str):
            detected = [detected]
        detected = [_text(item).lower() for item in (detected or []) if _text(item)]
        smart = [item for item in detected if item in detection_types]
        if not smart:
            is_motion_only = event_type in {"motion", "videoMotion"} or not detected
            if is_motion_only and "motion" in detection_types:
                # Plain motion events are included once 'motion' is selected;
                # they fall through the same camera filter and time checks below.
                smart = ["motion"]
            else:
                if is_motion_only:
                    skipped_motion_only += 1
                continue
        camera_id = _text(_first_value(row, "cameraId", "camera", "camera_id", "node"))
        camera_name = camera_names.get(camera_id, camera_id or "a camera")
        if camera_filter and camera_name.casefold() not in camera_filter and camera_id.casefold() not in camera_filter:
            continue
        moment = _ms_to_dt(_first_value(row, "start", "startTs", "createdAt", "timestamp", "at"))
        if moment is None:
            continue
        if not (start <= moment <= end):
            continue
        events.append(
            {
                "camera": camera_name,
                "types": smart,
                "at": moment.strftime("%-I:%M %p").lstrip("0").lower(),
                "iso": moment.isoformat(),
            }
        )

    events.sort(key=lambda item: item["iso"])
    events = events[:max_events]
    data = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "event_count": len(events),
        "skipped_motion_only": skipped_motion_only,
        "events": events,
        "events_unavailable": bool(error) and not events,
    }
    if error and not events:
        return {
            "ok": False,
            "summary": "",
            "data": data,
            "error": "This Protect console does not expose an events feed, so camera activity cannot be summarized.",
        }
    if not events:
        return {"ok": True, "summary": "", "data": data, "error": ""}

    bits = []
    for event in events:
        bits.append(f"{', '.join(event['types'])} on {event['camera']} around {event['at']}")
    summary = f"{len(events)} detection{'s' if len(events) != 1 else ''}: " + "; ".join(bits[:10]) + "."
    return {"ok": True, "summary": summary, "data": data, "error": ""}


# ---------------------------------------------------------------------------
# Verba
# ---------------------------------------------------------------------------


class OnDemandBriefingPlugin(ToolVerba):
    name = "on_demand_briefing"
    verba_name = "On Demand Briefing"
    pretty_name = "On Demand Briefing"
    version = "0.4.2"
    min_tater_version = "99"
    settings_category = SETTINGS_CATEGORY

    description = (
        "Run a spoken briefing on demand: morning briefing, daily briefing, security briefing, "
        "what happened while I was gone or out, what did I miss, welcome home briefing, "
        "rundown of the day, or a news briefing."
    )
    verba_dec = "Spoken briefings on demand with configurable sections: time, weather, news, camera activity, and BLE presence."
    when_to_use = (
        "Use when the user asks for a briefing or rundown: 'morning briefing', 'daily briefing', "
        "'security briefing', 'give me my briefing', 'what happened while I was gone/out/away', "
        "'welcome home briefing', 'what did I miss', 'news briefing', or names a configured briefing. "
        "Do not use for simple weather or clock questions."
    )
    how_to_use = (
        "Pass the user's request in query, or pass the briefing name in briefing when known. "
        "The verba resolves the briefing, runs its sections, and returns spoken text."
    )
    common_needs = ["Which briefing the user wants, when several are configured."]
    missing_info_prompts = ["Which briefing would you like?"]
    example_calls = [
        '{"function":"on_demand_briefing","arguments":{"query":"give me my morning briefing"}}',
        '{"function":"on_demand_briefing","arguments":{"briefing":"welcome_home","query":"what happened while I was gone"}}',
        '{"function":"on_demand_briefing","arguments":{"query":"give me the news briefing"}}',
    ]
    usage = '{"function":"on_demand_briefing","arguments":{"query":"give me my morning briefing"}}'
    argument_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The user's briefing request in their words."},
            "briefing": {"type": "string", "description": "Optional briefing id or name when the user named one."},
        },
        "required": ["query"],
    }
    routing_keywords = [
        "briefing",
        "morning briefing",
        "daily briefing",
        "security briefing",
        "while i was gone",
        "while i was out",
        "what did i miss",
        "rundown",
        "welcome home",
        "news briefing",
    ]

    platforms = ["voice_core", "webui", "homeassistant", "homekit", "xbmc", "macos", "little_spud"]
    tags = ["briefing", "weather", "news", "presence", "unifi protect"]
    notifier = False

    waiting_prompt_template = (
        "Write one short, natural message telling {mention} you are putting together their briefing. "
        "Only output the message."
    )

    required_settings = {
        "DEFAULT_IDENTITY": {
            "label": "Default Presence Identity",
            "type": "text",
            "default": "",
            "description": "Tracked device id or name (Satellites, Presence) used for away-window briefings that have no identity of their own.",
        },
        "PRESENCE_GRACE_MINUTES": {
            "label": "Presence Grace Period (minutes)",
            "type": "number",
            "default": 10,
            "description": "Absences shorter than this are treated as BLE dropouts, not departures.",
        },
        "MIN_ABSENCE_MINUTES": {
            "label": "Minimum Absence (minutes)",
            "type": "number",
            "default": 15,
            "description": "Absences shorter than this do not count as a departure.",
        },
        "LOOKBACK_HOURS": {
            "label": "Departure Lookback (hours)",
            "type": "number",
            "default": 48,
            "description": "How far back to search presence history for a departure.",
        },
        "DEFAULT_DETECTION_TYPES": {
            "label": "Default Camera Detection Types",
            "type": "multiselect",
            "default": DEFAULT_DETECTION_TYPES,
            "options": ["person", "vehicle", "package", "animal", "motion"],
            "description": "Smart detection types included when a briefing does not set its own. Generic motion is noise-filtered by default.",
        },
        "DEFAULT_STYLE": {
            "label": "Default TTS Style",
            "type": "select",
            "default": "brief",
            "options": ["brief", "detailed"],
            "description": "Length and tone used when a briefing does not set its own style.",
        },
        "BRIEFINGS_JSON": {
            "label": "Briefing Definitions (advanced JSON)",
            "type": "text",
            "default": "",
            "description": "Advanced raw JSON for briefing definitions (only shown if the per-briefing form editor is unavailable). Leave empty to use the built-in Morning and Welcome Home briefings. Each briefing supports: id, name, enabled, trigger_phrases, sections, time_window, prompt, style, empty_message, identity, section_options, delivery.",
        },
    }

    # ---------------- settings ----------------

    def _get_settings(self) -> Dict[str, str]:
        try:
            from helpers import redis_client

            raw = redis_client.hgetall(f"verba_settings:{self.settings_category}") or redis_client.hgetall(
                f"verba_settings: {self.settings_category}"
            )
        except Exception:
            return {}
        return _decode_redis_map(raw)

    def _briefings(self, settings: Dict[str, str]) -> Tuple[List[Dict[str, Any]], str]:
        raw = _text(settings.get("BRIEFINGS_JSON"))
        if not raw:
            return _default_briefings(), ""
        briefings, error = normalize_briefings(raw)
        if error:
            return _default_briefings(), error
        return briefings, ""

    def _global_setting(self, settings: Dict[str, str], key: str, default: str) -> str:
        value = _text(settings.get(key))
        return value or default

    # ---------------- web UI briefing form ----------------

    def webui_settings_fields(
        self,
        *,
        fields: List[Dict[str, Any]],
        current_settings: Optional[Dict[Any, Any]] = None,
        redis_client: Any = None,
        notifier_destination_catalog: Any = None,
    ) -> List[Dict[str, Any]]:
        """Replace the raw BRIEFINGS_JSON field with one editable group per
        briefing (plus an "Add a briefing" group). Falls back to the JSON
        field if anything goes wrong."""
        try:
            base = [dict(f) for f in (fields or []) if _text(f.get("key") if isinstance(f, dict) else None) != "BRIEFINGS_JSON"]
            briefings, _ = self._briefings(_decode_redis_map(current_settings or {}))
            return base + briefing_form_fields(briefings)
        except Exception:
            logger.exception("[on_demand_briefing] briefing form fields failed; falling back to JSON field")
            return fields

    def webui_prepare_settings_values(
        self,
        *,
        values: Dict[str, Any],
        redis_client: Any = None,
    ) -> Dict[str, Any]:
        """Turn the per-briefing form values back into a single BRIEFINGS_JSON
        setting, and drop the helper keys from what gets stored."""
        out = dict(values or {})
        try:
            base, _ = self._briefings(self._get_settings())
            rebuilt: List[Dict[str, Any]] = []
            for item in base:
                token = _text(item.get("id")) or str(len(rebuilt))
                prefix = _form_prefix(token)
                if f"{prefix}NAME" not in out and f"{prefix}REMOVE" not in out:
                    # Group was not rendered (e.g. stale form); keep unchanged.
                    rebuilt.append(dict(item))
                elif _to_bool(out.get(f"{prefix}REMOVE")):
                    continue
                else:
                    rebuilt.append(_apply_briefing_form(dict(item), out, prefix))

            new_name = _text(out.get(f"{_form_prefix(NEW_BRIEF_TOKEN)}NAME"))
            if new_name:
                rebuilt.append(_new_briefing_from_form(out, [b.get("id", "") for b in rebuilt]))

            briefings, error = normalize_briefings(rebuilt)
            if error:
                # Should not happen from form values; drop helpers and keep prior JSON.
                logger.warning("[on_demand_briefing] briefing form rebuild rejected: %s", error)
                return _drop_form_values(out)
            out["BRIEFINGS_JSON"] = json.dumps(briefings, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("[on_demand_briefing] briefing form rebuild failed")
        return _drop_form_values(out)

    # ---------------- window + identity ----------------

    async def _resolve_presence_identity(
        self,
        settings: Dict[str, str],
        briefing: Dict[str, Any],
        context: Optional[Dict[str, Any]],
    ) -> Tuple[str, str]:
        """Resolve the presence identity to a tracked device id.

        Order: briefing identity, satellite context hints (person name), then
        DEFAULT_IDENTITY. Each candidate is matched against the presence
        registry (device id, display name, owner) before falling back to the
        first raw candidate as a device id. Returns (device_id, label).
        """
        candidates: List[str] = []
        for candidate in (
            _text(briefing.get("identity")),
            _identity_from_context(context),
            self._global_setting(settings, "DEFAULT_IDENTITY", ""),
        ):
            candidate = _text(candidate)
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        if not candidates:
            return "", ""

        native_ble = _native_ble()
        if native_ble is not None:
            try:
                snapshot = await asyncio.to_thread(lambda: native_ble.snapshot(include_observations=False))
                devices = snapshot.get("devices") if isinstance(snapshot, dict) else []
                for candidate in candidates:
                    token = _text(candidate).casefold()
                    for device in devices or []:
                        if not isinstance(device, dict):
                            continue
                        device_id = _text(device.get("id"))
                        if not device_id:
                            continue
                        if any(_text(device.get(field)).casefold() == token for field in ("id", "display_name", "owner")):
                            return device_id, candidate
            except Exception as exc:
                logger.warning("[on-demand-briefing] presence identity lookup failed: %s", exc)

        return _text(candidates[0]).lower().replace(" ", "-"), candidates[0]

    async def _compute_away(
        self,
        settings: Dict[str, str],
        device_token: str,
    ) -> Dict[str, Any]:
        native_ble = _native_ble()
        if native_ble is None:
            return {"status": "no_data", "start": None, "end": None, "duration_s": None}
        grace_s = _to_int(settings.get("PRESENCE_GRACE_MINUTES"), 10, 0, 720) * 60
        min_absence_s = _to_int(settings.get("MIN_ABSENCE_MINUTES"), 15, 0, 720) * 60
        lookback_s = _to_int(settings.get("LOOKBACK_HOURS"), 48, 1, 720) * 3600

        token = _text(device_token)

        def _history():
            try:
                return native_ble.history_snapshot(device_id=token, limit=500)
            except TypeError:
                return native_ble.history_snapshot(device_id=token)

        try:
            history = await asyncio.to_thread(_history)
        except Exception as exc:
            logger.warning("[on-demand-briefing] presence history unavailable: %s", exc)
            return {"status": "no_data", "start": None, "end": None, "duration_s": None}

        events = history.get("events") if isinstance(history, dict) else history
        if not isinstance(events, list):
            events = []
        return compute_away_window(
            [row for row in events if isinstance(row, dict)],
            now_ts=_local_now().timestamp(),
            device_token=token,
            grace_s=float(grace_s),
            min_absence_s=float(min_absence_s),
            lookback_s=float(lookback_s),
        )

    # ---------------- summarization ----------------

    async def _summarize(
        self,
        briefing: Dict[str, Any],
        settings: Dict[str, str],
        section_results: Dict[str, Dict[str, Any]],
        window: Dict[str, Any],
        llm_client: Any,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        style = _text(briefing.get("style")) or self._global_setting(settings, "DEFAULT_STYLE", "brief")
        prompt = _text(briefing.get("prompt")) or (
            "Write a short spoken briefing from the provided section data."
        )
        empty_message = _text(briefing.get("empty_message"))

        payload = {
            "briefing": briefing.get("name"),
            "style": style,
            "window": window,
            "sections": {
                name: {
                    "ok": result.get("ok"),
                    "summary": result.get("summary"),
                    "data": result.get("data"),
                    "error": result.get("error"),
                }
                for name, result in section_results.items()
            },
        }

        deterministic_bits = []
        for name, result in section_results.items():
            bit = _text(result.get("summary"))
            if bit:
                deterministic_bits.append(bit)
        deterministic = " ".join(deterministic_bits).strip()

        has_content = any(result.get("ok") and (_text(result.get("summary")) or result.get("data")) for result in section_results.values())
        if not has_content:
            errors = [_text(result.get("error")) for result in section_results.values() if _text(result.get("error"))]
            if errors:
                return " ".join(errors[:2])
            return empty_message or "There is nothing to report right now."

        if llm_client is None:
            return deterministic or empty_message

        person_instructions = _person_instructions_from_context(context)
        address_line = (
            "- Address the user per these trusted person instructions from Settings > People: "
            f"{person_instructions}\n"
            if person_instructions
            else ""
        )
        system = (
            "You write short spoken briefings for Tater. Follow the briefing instructions.\n"
            "Rules:\n"
            "- Spoken-friendly: short sentences, natural times ('around ten past ten'), no markdown, no lists, no URLs.\n"
            "- Use ONLY the provided section data. Never fabricate events, names, numbers, or times.\n"
            f"{address_line}"
            "- If a section reported an error, briefly mention that piece was unavailable; still deliver the rest.\n"
            f"- Style: {style}.\n"
            "- If every section is empty, reply with exactly: EMPTY\n"
            "Briefing instructions:\n"
            f"{prompt}"
        )
        try:
            response = await llm_client.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
                ],
                temperature=0.2,
                max_tokens=650,
            )
            text = spoken_flatten((response.get("message") or {}).get("content"))
        except Exception as exc:
            logger.warning("[on_demand_briefing] LLM summary failed, using deterministic fallback: %s", exc)
            return deterministic or empty_message or "The briefing could not be summarized."

        if text.casefold() == "empty":
            return empty_message or "There is nothing to report right now."
        return text or deterministic or empty_message

    # ---------------- delivery ----------------

    def _announcement_targets(self, delivery: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> List[str]:
        raw_targets = delivery.get("targets")
        if isinstance(raw_targets, list) and raw_targets:
            try:
                from announcement_targets import normalize_announcement_targets

                return [t for t in normalize_announcement_targets(raw_targets) if _text(t)]
            except Exception:
                return [_text(t) for t in raw_targets if _text(t)]

        if _to_bool(delivery.get("requester_target"), False):
            selector = _requesting_satellite_selector(context)
            if selector:
                try:
                    from announcement_targets import VOICE_CORE_TARGET_PREFIX, normalize_announcement_targets

                    return [t for t in normalize_announcement_targets([f"{VOICE_CORE_TARGET_PREFIX}{selector}"]) if _text(t)]
                except Exception:
                    return [f"voice_core:{selector}"]

        try:
            from announcement_targets import VOICE_CORE_TARGET_PREFIX, get_voice_core_satellite_target_options

            rows = get_voice_core_satellite_target_options(current_values=[])
            targets = [
                str(row.get("value") or "").strip()
                for row in rows
                if str(row.get("value") or "").strip().startswith(VOICE_CORE_TARGET_PREFIX)
            ]
            return [t for t in targets if t]
        except Exception:
            return []

    def _homeassistant_config(self) -> Dict[str, Any]:
        try:
            from tateros import integration_store as integration_store_module

            module = integration_store_module.integration_module("homeassistant")
            if module is not None:
                return module.load_homeassistant_config(required=False) or {}
        except Exception:
            pass
        return {"base": "", "token": ""}

    async def _deliver_announcement(self, text: str, delivery: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        targets = self._announcement_targets(delivery, context)
        if not targets:
            return {"ok": False, "error": "No Tater satellites are connected, so the briefing could not be announced."}

        from speech_settings import get_speech_settings
        from speech_tts import speak_announcement_targets

        speech = get_speech_settings() or {}
        ha = self._homeassistant_config()
        announcement_backend = str(speech.get("announcement_tts_backend") or speech.get("tts_backend") or "wyoming")
        scene = normalize_audio_scene(delivery.get("background_audio") or delivery.get("audio_scene"))

        try:
            result = await speak_announcement_targets(
                text=text,
                backend=announcement_backend,
                ha_base=str(ha.get("base") or ""),
                token=str(ha.get("token") or ""),
                targets=targets,
                model=str(speech.get("announcement_tts_model") or ""),
                voice=str(speech.get("announcement_tts_voice") or ""),
                wyoming_host=str(speech.get("wyoming_tts_host") or ""),
                wyoming_port=speech.get("wyoming_tts_port"),
                wyoming_voice=str(speech.get("wyoming_tts_voice") or ""),
                voice_core_backend=str(speech.get("tts_backend") or ""),
                voice_core_model=str(speech.get("tts_model") or ""),
                voice_core_voice=str(speech.get("tts_voice") or ""),
                voice_core_wyoming_host=str(speech.get("wyoming_tts_host") or ""),
                voice_core_wyoming_port=speech.get("wyoming_tts_port"),
                voice_core_wyoming_voice=str(speech.get("wyoming_tts_voice") or ""),
                default_backend=announcement_backend,
                audio_scene=scene,
            )
        except Exception as exc:
            logger.error("[on_demand_briefing] announcement TTS call failed: %s", exc)
            return {"ok": False, "error": f"The briefing announcement failed: {exc}"}

        sent = int(result.get("sent_count") or 0)
        if not result.get("ok") or sent <= 0:
            return {"ok": False, "error": _text(result.get("error")) or "The briefing announcement failed on all targets."}
        return {
            "ok": True,
            "sent_count": sent,
            "target_count": int(result.get("target_count") or len(targets)),
            "audio_scene_sent_count": int(result.get("audio_scene_sent_count") or 0),
            "audio_scene_fallback_count": int(result.get("audio_scene_fallback_count") or 0),
        }

    # ---------------- core ----------------

    async def _handle(self, args: Dict[str, Any], llm_client: Any, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = args or {}
        settings = self._get_settings()
        briefings, config_error = self._briefings(settings)
        if config_error:
            return action_failure(
                code="briefing_config_invalid",
                message=f"The On Demand Briefing settings could not be read: {config_error}",
                say_hint="Explain the briefing configuration is invalid and point to the On Demand Briefing settings.",
            )

        query = _text(args.get("query") or args.get("request") or args.get("text"))
        briefing, match_error = match_briefing(briefings, query=query, explicit=_text(args.get("briefing")))
        if briefing is None:
            return action_failure(
                code="briefing_not_found",
                message=match_error,
                needs=[match_error] if match_error.endswith("?") else ["Ask which briefing the user wants."],
                say_hint="Ask which briefing the user wants.",
            )

        section_names = [_text(s).lower().replace("-", "_") for s in briefing.get("sections", [])]
        window_config = briefing.get("time_window") or {}
        strategy = _text(window_config.get("strategy")).lower()

        away = {"status": "no_data", "start": None, "end": None, "duration_s": None}
        needs_away = strategy == "last_away_period" or "presence" in section_names
        if needs_away:
            device_token, _identity_label = await self._resolve_presence_identity(settings, briefing, context)
            away = await self._compute_away(settings, device_token)

        now = _local_now()
        window_required = "camera_activity" in section_names or strategy in {"since_time", "last_n_hours", "last_away_period"}
        start = end = None
        window_error = ""
        if window_required:
            start, end, window_error = resolve_window(window_config, away, now=now)
        window = {
            "strategy": strategy or ("last_away_period" if needs_away else "none"),
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
        }
        if window_required and (start is None or end is None):
            detail = window_error or "The briefing time window could not be resolved."
            if "presence" in detail.lower() or strategy == "last_away_period":
                return action_failure(
                    code="briefing_window_unavailable",
                    message=detail,
                    say_hint="Explain that presence data for that person is unavailable or shows no departure.",
                )
            return action_failure(code="briefing_window_invalid", message=detail)

        runtime = {
            "llm_client": llm_client,
            "settings": settings,
            "away_window": away,
            "protect_client": None,
        }
        if "camera_activity" in section_names:
            runtime["protect_client"] = _protect_client()

        section_results: Dict[str, Dict[str, Any]] = {}
        for name in section_names:
            provider = SECTION_PROVIDERS.get(name)
            if provider is None:
                section_results[name] = {"ok": False, "summary": "", "data": {}, "error": f"Unknown section '{name}'."}
                continue
            try:
                section_results[name] = await provider(briefing, window, runtime)
            except Exception as exc:
                logger.exception("[on_demand_briefing] section '%s' failed", name)
                section_results[name] = {"ok": False, "summary": "", "data": {}, "error": f"The {name} section failed: {exc}"}

        briefing_text = await self._summarize(briefing, settings, section_results, window, llm_client, context)

        delivery = briefing.get("delivery") or {}
        mode = _text(delivery.get("mode")).lower() or "response"
        if mode == "announce":
            delivered = await self._deliver_announcement(briefing_text, delivery, context)
            if not delivered.get("ok"):
                return action_failure(
                    code="briefing_announce_failed",
                    message=_text(delivered.get("error")) or "The briefing announcement failed.",
                    say_hint="Explain the briefing could not be announced.",
                )
            fallback_note = ""
            if delivered.get("audio_scene_fallback_count"):
                fallback_note = " Some satellites played it without the background audio."
            return action_success(
                facts={
                    "briefing": briefing.get("id"),
                    "delivered_to": delivered.get("sent_count"),
                    "target_count": delivered.get("target_count"),
                    "background_audio": bool(normalize_audio_scene(delivery.get("background_audio") or delivery.get("audio_scene"))),
                    "sections": {name: result.get("ok") for name, result in section_results.items()},
                },
                data={"briefing_text": briefing_text, "sections": {name: result.get("data") for name, result in section_results.items()}},
                summary_for_user=f"Your {briefing.get('name')} was delivered to {delivered.get('sent_count')} target{'s' if delivered.get('sent_count') != 1 else ''}." + fallback_note,
                say_hint="Briefly confirm the briefing was just announced. Do not repeat the briefing text.",
            )

        return action_success(
            facts={"briefing": briefing.get("id"), "sections": {name: result.get("ok") for name, result in section_results.items()}},
            data={"sections": {name: result.get("data") for name, result in section_results.items()}},
            summary_for_user=briefing_text,
            say_hint="Read this briefing aloud verbatim without adding unverified details.",
        )

    # ---------------- platform handlers ----------------

    async def handle_voice_core(self, args=None, llm_client=None, context=None, *unused_args, **unused_kwargs):
        return await self._handle(args or {}, llm_client, context)

    async def handle_homeassistant(self, args, llm_client, context=None):
        return await self._handle(args or {}, llm_client, context)

    async def handle_webui(self, args, llm_client, context=None):
        return await self._handle(args or {}, llm_client, context)

    async def handle_little_spud(self, args=None, llm_client=None, context=None, *unused_args, **unused_kwargs):
        return await self._handle(args or {}, llm_client, context)

    async def handle_macos(self, args, llm_client, context=None):
        return await self._handle(args or {}, llm_client, context)

    async def handle_homekit(self, args, llm_client, context=None):
        return await self._handle(args or {}, llm_client, context)

    async def handle_xbmc(self, args, llm_client, context=None):
        return await self._handle(args or {}, llm_client, context)

    async def handle_discord(self, message, args, llm_client, context=None):
        return await self._handle(args or {}, llm_client, context)


verba = OnDemandBriefingPlugin()
