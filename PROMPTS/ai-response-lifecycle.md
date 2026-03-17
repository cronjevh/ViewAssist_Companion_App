---
title: "AI Response Card Lifecycle"
sidebar_position: 6
---

# Goal

Keep AI response UX deterministic:

1. Show streamed text immediately.
2. Clear the info card within 30 seconds of the **final** response event.
3. Recover if the display occasionally stays on `/view-assist/info` instead of returning home (for example `/view-assist/clock`).

# Symptom

You may see `sensor.vaca_*_current_path` transitions like:

- `/view-assist/clock` -> `/view-assist/info` -> `/view-assist/clock` (expected)
- Sometimes `/view-assist/info` persists (intermittent stuck case)

# Why This Happens

Two different timing paths are involved:

1. Card content lifecycle (your automation/scripts).
2. View Assist navigation revert timer (inside View Assist integration).

Common stuck reasons:

- Reverting modes (`normal`, `night`, `music`) were not active.
- Revert task was cancelled/replaced by later navigation.
- Browser navigation event was missed/interrupted.

`sensor.vaca_*_current_path` reflects what happened; it does not control navigation.

# Deterministic Pattern

Use 3 scripts with clear responsibilities:

1. `script.va_ai_response_card_update`
   - Updates title/message on every chunk.
   - Starts expiry and navigation watchdog only on `is_final: true`.
2. `script.va_ai_response_card_expire`
   - `mode: restart`
   - Clears card exactly once, 30s after final event (if `run_id` still matches).
3. `script.va_ai_response_info_watchdog`
   - `mode: restart`
   - After timeout, checks whether screen is still on `/view-assist/info` in a reverting mode.
   - If yes, forces `view_assist.navigate` back to home view.

# HA Objects Involved

- View Assist entity, for example: `sensor.viewassist_kitchen`
- (Optional) mirrored VACA path sensor, for example: `sensor.vaca_215d3e5be_current_path`
- Ingest automation that receives AI chunks/final events
- Scripts:
  - `script.va_ai_response_card_update`
  - `script.va_ai_response_card_expire`
  - `script.va_ai_response_info_watchdog`

# Scripts (Copy/Paste)

```yaml
script:
  va_ai_response_card_update:
    alias: VA AI Response Card Update
    mode: parallel
    fields:
      va_entity:
        description: Target View Assist entity id
        example: sensor.viewassist_kitchen
      response_id:
        description: Stable id for this response/run/conversation
        example: 26c937a8-31ea-423b-a806-1f8f8f79a9c8
      response_text:
        description: Current response text (chunk or full)
      is_final:
        description: true only for final response event
        example: false
      card_title:
        description: Card title to show
        default: AI Response
      message_font_size:
        description: Optional card font size
        default: 4vw
      clear_after:
        description: Seconds before clearing the card
        default: 30
      revert_after:
        description: Seconds before forcing home if still on info
        default: 35
      info_path:
        description: Info view path
        default: /view-assist/info
      home_path:
        description: Home/default path
        default: /view-assist/clock
      path_sensor:
        description: Optional path sensor if va_entity has no current_path attribute
        example: sensor.vaca_215d3e5be_current_path
    sequence:
      - action: view_assist.set_state
        target:
          entity_id: "{{ va_entity }}"
        data:
          title: "{{ card_title | default('AI Response') }}"
          message: "{{ response_text }}"
          message_font_size: "{{ message_font_size | default('4vw') }}"
          ai_response_run_id: "{{ response_id }}"

      - if:
          - condition: template
            value_template: "{{ is_final | bool }}"
        then:
          - action: view_assist.set_state
            target:
              entity_id: "{{ va_entity }}"
            data:
              ai_response_final_ts: "{{ now().isoformat() }}"

          - action: logbook.log
            data:
              name: VA AI Response
              entity_id: "{{ va_entity }}"
              message: "Final response event run_id={{ response_id }} at {{ now().isoformat() }}"

          - action: script.va_ai_response_card_expire
            data:
              va_entity: "{{ va_entity }}"
              response_id: "{{ response_id }}"
              clear_after: "{{ clear_after | int(30) }}"

          - action: script.va_ai_response_info_watchdog
            data:
              va_entity: "{{ va_entity }}"
              response_id: "{{ response_id }}"
              path_sensor: "{{ path_sensor | default('') }}"
              info_path: "{{ info_path | default('/view-assist/info') }}"
              home_path: "{{ home_path | default('/view-assist/clock') }}"
              revert_after: "{{ revert_after | int(35) }}"

  va_ai_response_card_expire:
    alias: VA AI Response Card Expire
    mode: restart
    fields:
      va_entity:
        description: Target View Assist entity id
      response_id:
        description: Response id captured at final event
      clear_after:
        description: Seconds before clearing (capped to 30)
        default: 30
    sequence:
      - delay:
          seconds: "{{ [[clear_after | int(30), 0] | max, 30] | min }}"

      - condition: template
        value_template: "{{ state_attr(va_entity, 'ai_response_run_id') == response_id }}"

      - variables:
          elapsed_seconds: >-
            {% set ts = state_attr(va_entity, 'ai_response_final_ts') %}
            {% if ts %}
              {{ (as_timestamp(now()) - as_timestamp(ts)) | round(2) }}
            {% else %}
              unknown
            {% endif %}

      - action: view_assist.set_state
        target:
          entity_id: "{{ va_entity }}"
        data:
          title: ""
          message: ""
          ai_response_run_id: ""
          ai_response_final_ts: ""

      - action: logbook.log
        data:
          name: VA AI Response
          entity_id: "{{ va_entity }}"
          message: "Card cleared run_id={{ response_id }} elapsed={{ elapsed_seconds }}s"

  va_ai_response_info_watchdog:
    alias: VA AI Response Info Watchdog
    mode: restart
    fields:
      va_entity:
        description: Target View Assist entity id
      response_id:
        description: Response id captured at final event
      revert_after:
        description: Seconds to wait before watchdog check
        default: 35
      info_path:
        description: Info view path
        default: /view-assist/info
      home_path:
        description: Home/default path
        default: /view-assist/clock
      path_sensor:
        description: Optional fallback sensor entity for current path
        default: ""
    sequence:
      - delay:
          seconds: "{{ [[revert_after | int(35), 1] | max, 300] | min }}"

      - condition: template
        value_template: "{{ state_attr(va_entity, 'ai_response_run_id') == response_id }}"

      - variables:
          current_mode: "{{ (state_attr(va_entity, 'mode') or 'normal') | lower }}"
          current_path: >-
            {% set va_path = state_attr(va_entity, 'current_path') %}
            {% if va_path %}
              {{ va_path }}
            {% elif (path_sensor | default('') | length) > 0 %}
              {{ states(path_sensor) }}
            {% else %}
              unknown
            {% endif %}

      - condition: template
        value_template: "{{ current_mode in ['normal', 'night', 'music'] }}"

      - condition: template
        value_template: "{{ current_path == (info_path | default('/view-assist/info')) }}"

      - action: view_assist.navigate
        data:
          device: "{{ va_entity }}"
          path: "{{ home_path | default('/view-assist/clock') }}"

      - action: logbook.log
        data:
          name: VA AI Response
          entity_id: "{{ va_entity }}"
          message: "Watchdog forced navigation to {{ home_path }} for run_id={{ response_id }} from path={{ current_path }} mode={{ current_mode }}"
```

# Ingest Automation Pattern

Call `script.va_ai_response_card_update` from your AI response ingest automation:

```yaml
automation:
  - alias: VA AI Response Card Ingest
    id: va_ai_response_card_ingest
    mode: queued
    max: 50
    triggers:
      - trigger: event
        event_type: your_ai_response_event
    actions:
      - variables:
          va_entity: "{{ trigger.event.data.va_entity }}"
          response_id: "{{ trigger.event.data.response_id }}"
          response_text: "{{ trigger.event.data.response_text }}"
          is_final: "{{ trigger.event.data.is_final | bool }}"
          path_sensor: "{{ trigger.event.data.path_sensor | default('') }}"

      - action: script.va_ai_response_card_update
        data:
          va_entity: "{{ va_entity }}"
          response_id: "{{ response_id }}"
          response_text: "{{ response_text }}"
          is_final: "{{ is_final }}"
          card_title: AI Response
          message_font_size: 4vw
          clear_after: 30
          revert_after: 35
          info_path: /view-assist/info
          home_path: /view-assist/clock
          path_sensor: "{{ path_sensor }}"
```

# Rules

- Do not put `delay + clear` logic in ingest automation.
- Only start expiry/watchdog on final event.
- Use `mode: restart` on expiry/watchdog scripts so each run has one active timer.
- Keep mode-aware behavior:
  - `normal`, `night`, `music` should revert.
  - `hold`, `cycle` should not be force-reverted.

# Validation Checklist

1. Confirm final response log appears:
   - `Final response event run_id=...`
2. Confirm card clears in ~30s:
   - `Card cleared run_id=... elapsed=...s`
3. For forced cases only, confirm watchdog log appears:
   - `Watchdog forced navigation ...`
4. Verify `sensor.vaca_*_current_path` returns from `/view-assist/info` to home path.

# Notes

- `view_assist.navigate` service expects:
  - `device` = View Assist entity id (for example `sensor.viewassist_kitchen`)
  - `path` = dashboard path (for example `/view-assist/clock`)
- If your View Assist entity already exposes `current_path`, `path_sensor` is optional.
