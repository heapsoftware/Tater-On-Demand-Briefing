# Changelog

Short summary of what changed in each release. Versions follow
`MAJOR.MINOR.REVISION` (starting at 0.1.0); each version is tagged `vX.Y.Z`
and posted as a GitHub release with these notes.

## v0.2.0 — 2026-09-30

- **Features** — Briefing definitions are now editable from the web UI with per-briefing form fields (enabled, trigger phrases, sections, time window, prompt, style, delivery, news, weather and camera options) plus an "Add a briefing" group — no JSON required. Briefings can also be removed or disabled with checkboxes.
- **Changes** — Custom briefings are stored under `verba/on_demand_briefing.py` in the repository (no functional change to installed verbas); an explicitly empty briefing list now means "no briefings" instead of falling back to the built-ins.

## v0.1.0 — 2026-09-30

Initial release.

- **Features** — Two built-in briefings: Morning Briefing (time, weather, overnight camera activity since 10 PM) and Welcome Home Briefing (presence + camera activity over your most recent away period).
- **Features** — Pluggable sections: time, weather (WeatherAPI), news (configurable topic via web search with configurable length), UniFi Protect camera activity (smart-detection filtered), and BLE presence from native history.
- **Features** — Briefings are data: configure trigger phrases, sections, time windows, prompts, styles, and per-section options as JSON in settings — no code changes to add a briefing.
- **Features** — Optional `announce` delivery mode with looping background audio ducked under the TTS stream (same mechanism as the AI Task core; supports `/api/ai-tasks/background-audio/` assets).
- **Behavior** — Graceful partial success: a failing section (e.g. Protect consoles without an events feed) is reported as unavailable while the rest of the briefing still delivers; the briefing never fabricates events.
