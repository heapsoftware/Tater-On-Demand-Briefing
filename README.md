# On Demand Briefing — a Tater Verba

Spoken briefings on demand for [Tater](https://github.com/TaterTotterson/Tater):
morning briefings, "what happened while I was gone" security briefings, news
briefings, and anything else you configure — each made of pluggable sections
(time, weather, news, UniFi Protect camera activity, and BLE presence).

## What it does

Ask your Tater satellite things like:

- "Morning briefing" / "Daily briefing" / "Rundown of the day"
- "What happened while I was gone?" / "Welcome home briefing" / "What did I miss?"
- "Give me the news briefing"

Two briefings ship built-in:

| Briefing | Sections | Window |
| --- | --- | --- |
| **Morning Briefing** | time, weather, camera_activity | Since 10:00 PM last night (configurable) |
| **Welcome Home Briefing** | presence, camera_activity | Your most recent away period |

Every briefing is a data record, so you can add your own (e.g. a news briefing)
from the settings page without touching code.

### Sections (pluggable providers)

- **time** — current local date and time, spoken naturally.
- **weather** — current conditions via the **WeatherAPI** integration (real-time
  only by default; forecast is per-briefing opt-in).
- **news** — searches the web for a topic you type in (uses Tater's web search
  tool) and summarizes the results into a few spoken sentences, with a
  configurable length and item count.
- **camera_activity** — UniFi Protect smart-detection events (person, vehicle,
  package, animal) in the window. Generic motion is noise-filtered by default;
  add **motion** to a briefing's camera detection types to also report plain
  motion events (with their times and cameras) to the summary.
- **presence** — "You were away from about 8:05 am to 4:40 pm, roughly 8 and a
  half hours", derived from Tater's native BLE presence history.

### Background audio (movie-like announcements)

Any briefing can use the `announce` delivery mode: the briefing is spoken
through the announcement path with optional **looping background audio ducked
under the TTS stream** — the same mechanism the AI Task core uses. Upload a
WAV/MP3/FLAC (up to 16 MB) directly in the briefing form, or use any audio
asset under `/api/ai-tasks/background-audio/` (presets or uploads) as the
background URL. Announce briefings can play on every connected satellite, on
an explicit target list, or on **the satellite you asked from** (leave the
target list empty and tick "Announce on the asking satellite"). By default
briefings use the plain `response` path.

## Setup

1. **Install the verba** — in Tater's UI, go to **Settings → Verba** and add
   this repository's URL under **Custom Verba repositories**:

   ```
   https://raw.githubusercontent.com/heapsoftware/Tater-On-Demand-Briefing/main/manifest.json
   ```

   (Or use the GitHub URL `https://github.com/heapsoftware/Tater-On-Demand-Briefing`
   if your Tater build accepts repository roots.) Install **On Demand Briefing**
   from the shop list. Tater verifies the file's SHA-256 from the manifest and
   installs it as `verba/on_demand_briefing.py`.

2. **Configure integrations the sections use** (only what you enable):
   - *WeatherAPI* — Settings → Integrations → WeatherAPI (API key + default
     location) for the weather section.
   - *UniFi Protect* — Settings → Integrations → UniFi Protect for the camera
     activity section. Note: some Protect consoles do not expose an events
     feed; the briefing degrades gracefully and tells you so.
   - *BLE presence* — no setup beyond having tracked devices in Tater's
     Satellites / Presence page.

3. **Configure the verba** — Settings → Verba → **On Demand Briefing**:
   - **Default Presence Identity** — which tracked device defines "away" for
     the Welcome Home briefing (device id or name from Satellites/Presence).
     The verba also uses per-user identity hints from the requesting satellite
     when available.
   - **Presence Grace Period** (default 10 min) — absences shorter than this
     are treated as BLE dropouts, not departures.
   - **Minimum Absence** (default 15 min) — shorter absences don't count.
   - **Departure Lookback** (default 48 h) — how far back to search for a departure.
   - **Default Camera Detection Types** — person / vehicle / package / animal
     (motion available but noise-filtered by default).
   - **Default TTS Style** — brief or detailed.
   - **Briefing Definitions** — in the web UI, each briefing gets its own group
     of form fields (enabled, trigger phrases, sections, time window, prompt,
     style, delivery with announce targets and background audio, news, weather
     and camera options) plus an **Add a briefing** group — no JSON needed
     (see below).

4. **Try it**: "Hey Tater, give me my morning briefing."

Prompts can also use your **Settings > People** response instructions: Tater
attaches the asking person's instructions (e.g. "always call me sir") to the
request, and the briefing summary uses them when addressing you.

## Adding your own briefing

Open **Verba → On Demand Briefing → Settings** in the web UI. Every briefing
has its own editable group: change the fields, tick **Remove this briefing**
to delete it, and fill in the **Add a briefing** group (name plus sections
and options) to create a new one. Settings save on **Save settings**; newly
created briefings get their own group the next time you open settings.

### Advanced: raw JSON definitions

Everything the form covers (including announce targets and background audio)
is settable in the UI, so raw JSON is rarely needed. Briefings are stored as
a JSON array (`BRIEFINGS_JSON`); the raw field only appears in settings if
the form editor is unavailable. For reference, this is a news briefing with
background audio:

```json
[
  {
    "id": "news",
    "name": "News Briefing",
    "enabled": true,
    "trigger_phrases": ["news briefing", "the news"],
    "sections": ["time", "news"],
    "time_window": {"strategy": "last_n_hours", "hours": 24},
    "prompt": "Open with the time, then summarize the news in a neutral tone.",
    "style": "brief",
    "empty_message": "There is no news to report right now.",
    "identity": "",
    "section_options": {
      "news": {
        "topic": "technology",
        "max_items": 5,
        "max_sentences": 2
      }
    },
    "delivery": {
      "mode": "announce",
      "background_audio": {
        "background": {
          "url": "/api/ai-tasks/background-audio/presets/news.wav",
          "loop": true,
          "volume_percent": 60
        }
      }
    }
  }
]
```

Field reference per briefing:

| Field | Meaning |
| --- | --- |
| `id` | Unique slug used for matching and in facts. |
| `name` | Display name (also matchable). |
| `enabled` | Toggle without deleting. |
| `trigger_phrases` | Extra phrases that route to this briefing. |
| `sections` | Ordered list: `time`, `weather`, `news`, `camera_activity`, `presence`. |
| `time_window` | `{"strategy": "since_time", "start_time": "22:00", "end_time": "now"}`, `{"strategy": "last_away_period"}`, or `{"strategy": "last_n_hours", "hours": 4}`. |
| `prompt` | Instructions for the LLM that turns section data into speech. |
| `style` | `brief` or `detailed`. |
| `empty_message` | What to say when nothing happened. |
| `identity` | Presence device for `last_away_period` (blank = default). |
| `section_options` | Per-section options (`news.topic/max_items/max_sentences`, `weather.location/units/include_forecast`, `camera_activity.cameras/detection_types/max_events`). |
| `delivery` | `{"mode": "response"}` (default) or `{"mode": "announce", "targets": [...], "background_audio": {"background": {"url": "...", "loop": true, "volume_percent": 60}}}`. |

## Notes on behavior

- **Never fabricates events.** The LLM may only use the section data it is
  given; if a section errors, the briefing mentions the gap and continues.
- **Partial success is normal.** A failed section (e.g. Protect events
  unavailable on some consoles) never blocks the whole briefing.
- **Privacy**: presence and camera data stay in Tater; nothing is sent to
  third parties beyond your configured TTS/LLM and WeatherAPI/news lookups.

## Releases

See [CHANGELOG.md](CHANGELOG.md). Versions follow `MAJOR.MINOR.REVISION`
starting at `0.1.0`; each release is tagged `vX.Y.Z` on GitHub.

## License

Same license as Tater. Built for the Tater verba plugin system.
