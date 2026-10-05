# Changelog

Short summary of what changed in each release. Versions follow
`MAJOR.MINOR.REVISION` (starting at 0.1.0); each version is tagged `vX.Y.Z`
and posted as a GitHub release with these notes.

## v0.5.1 — 2026-10-05

- **Fixes** — Morning briefing requests can fail intermittently because the Tater planner (Astraeus) occasionally classifies them as chat instead of a tool call, so the assistant replies "one moment" and nothing runs. The planner-facing tool description now leads with an imperative and the exact phrases people actually say ("morning briefing", "give my morning briefing", "give me my morning briefing") so they stay inside the 80-character catalog row the planner sees; `routing_keywords` gains the same phrases. No behavioral change for already-routed briefings.

## v0.5.0 — 2026-10-01

- **Features** — New per-briefing "TTS start delay (ms)" setting for announce-mode briefings: the background audio now plays at full volume for that many milliseconds before the spoken briefing starts and ducks it in (the "music lead-in"). Stored as `delivery.background_audio.foreground.start_delay_ms`, clamped to 0–30000, default 0 (current behavior). Honored by Tater v1.2.5+, which renders the whole scene server-side; on older Tater the key is ignored and the voice starts immediately.
- **Features** — New per-briefing "Music volume while speaking (percent)" setting to tune how loud the background audio stays while the voice is speaking. Stored as `delivery.background_audio.ducking.target_percent`, clamped to 0–300, default 35. 100 keeps the music unducked; values above 100 boost it above its normal level (needs a Tater core with the extended volume range — standard cores cap at 100).
- **Changes** — `normalize_audio_scene` now passes `foreground.start_delay_ms` through and allows `ducking.target_percent` up to 300 (was capped at 100).

## v0.4.5 — 2026-10-01

- **Features** — Announced briefings that play on exactly the satellite you asked from now reply first ("Your Morning Briefing will begin shortly.") and play the announcement right after the reply, instead of the briefing playing during the tool call and a redundant confirmation following it. The heavy work (sections, summary, announcement) runs as a background job; failures are spoken on the same satellite so the ack is never followed by unexplained silence. The `speak: false` interim from v0.4.4 is superseded on this path and no longer set (see `tts-silent-tool-reply-spec.md`).
- **Features** — New per-briefing "Reply line before the briefing" setting (`delivery.ack_line`, form field for announce-mode briefings) to customize the spoken line; `{name}` substitutes the briefing name, and empty uses the default "Your {name} briefing will begin shortly." A companion "Let the assistant write the reply line" checkbox (`delivery.ack_llm`) has the assistant compose its own one-sentence reply instead, using the asking user's trusted person instructions from Settings > People (e.g. sir or ma'am).
- **Changes** — Briefings announced to other satellites keep the current behavior (announcement during the tool call, spoken confirmation on the asking satellite).

## v0.4.4 — 2026-10-01

- **Fixes** — Announced briefings no longer get a follow-up reply that talks as if playback is pending ("Shall I begin the readout?"): the tool result now states plainly that the briefing announced itself aloud over the satellite speakers during the tool call, the response instructions forbid offering or asking to play it again, and the planner-facing tool description carries the same rule.
- **Features** — When an announced briefing plays on *exactly* the satellite you asked from, the result is marked `speak: false` and the reply instructions say to stay silent: the briefing audio already played right there and no confirmation is wanted. Briefings announced to other satellites keep today's spoken confirmation. The `speak: false` contract needs a small Tater core change (spec'd in `tts-silent-tool-reply-spec.md`) and is inert until that lands; the summary wording makes the interim reply as short as possible.
- **Changes** — Also surfaces background-audio warnings in the result and adds `played_on_requesting_satellite`, `background_audio_started` / `background_audio_fallback` facts so a silent music track is diagnosable.

## v0.4.3 — 2026-09-30

- **Features** — Uploaded background audio cleans itself up: saving settings now deletes files that the previous briefing definitions referenced and the new ones no longer do (briefing removed, background music cleared, or a file swapped for a new upload). Deletion is guarded — a file stays put while any remaining briefing points at it, when AI Task core Redis data mentions it, and whenever the usage scan cannot complete.

## v0.4.2 — 2026-09-30

- **Fixes** — Completes the version fix so the shop stops offering an update after install: v0.4.1 corrected the stale embedded version but set it to `0.4.0` while shipping it in a `0.4.1` manifest. The embedded version now matches the manifest (`0.4.2`) exactly, and the regression test keeps them pinned together.

## v0.4.1 — 2026-09-30

- **Fixes** — The installed verba now reports its matching version (`0.4.x`) to Tater: the plugin class carried a stale hardcoded `version = "0.2.1"` attribute from v0.3.0 onward, so updating appeared to succeed but the shop UI kept showing 0.2.1 with an update button. A dev regression test now pins the class version to the manifest version.

## v0.4.0 — 2026-09-30

- **Features** — Announce briefings can play on **the satellite you asked from**: with announce delivery and an empty target list, a new "Announce on the asking satellite" checkbox resolves the requesting Tater satellite from the trusted portal origin (explicit targets still take precedence; unchanged "all satellites" behavior otherwise).
- **Features** — The briefing form gains an **Upload Background Audio** field (WAV/MP3/FLAC up to 16 MB), storing uploads in the same shared Agent Lab folder the AI Task core uses and selecting the resulting asset as that briefing's background audio.

## v0.3.0 — 2026-09-30

- **Features** — Camera briefings can now report plain motion events: adding **motion** to a briefing's camera detection types includes motion-only events (with times and cameras) in the summary instead of silently dropping them as noise.
- **Features** — Briefing summaries now use the asking person's trusted response instructions from Settings > People (e.g. an honorific like "always call me sir") when addressing the user, so prompts saying "their honorific" work in both response and announce delivery.

## v0.2.1 — 2026-09-30

- **Fixes** — The briefing form editor no longer drops a JSON-authored `background_audio` scene when a briefing is saved, and background audio is settable directly in the UI (URL, loop, volume) for announce-delivery briefings. Also documented the correct nested `background_audio.background` shape (the previously documented flat `url` shape never produced audio).
- **Docs** — README now reflects the form-based briefing editor everywhere and lists weather options among the form fields.

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
