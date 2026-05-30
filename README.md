# Jebao Aquarium Pumps for Home Assistant

Home Assistant custom integration for Jebao aquarium pumps. Supports the
**MDP-20000** wavemaker over local LAN and the **MD-4.4** dosing pump
through the Gizwits cloud API.

## Features

- 🔍 **Automatic Discovery (MDP-20000)** — scans all network interfaces
- 🌐 **Multi-Subnet Support** — works across VLANs and IoT networks
- 🔌 **Local Control (MDP-20000)** — direct TCP, no cloud required
- ☁ **Cloud Control (MD-4.4)** — full app-equivalent control of dosing,
  schedules and calibration through Gizwits
- ⚡ **Real-Time Status** — state, speed, schedules, alerts
- 🐟 **Feed Mode (MDP-20000)** — temporary pause with auto-resume
- 💧 **Dose Scheduling (MD-4.4)** — read/write up to 24 entries per
  channel, plus a 10× precision mode for sub-mL dosing
- 🏠 **Native HA Integration** — config UI, device registry, services
  with response data

## Supported Models

- **Jebao MDP-20000** — variable-speed circulation pump. Controlled
  locally over your LAN (TCP/12416). No cloud or internet required.
- **Jebao MD-4.4** — 4-channel dosing pump. Controlled through the
  Gizwits cloud REST API at `usapi.gizwits.com` (or your region's
  equivalent), which is the same backend the official Jebao Aqua app
  uses. LAN control isn't viable because the shipping firmware
  acknowledges but silently drops local write commands — cloud writes
  go through without issue.

## Installation

### HACS (Recommended)

1. Open HACS
2. Go to **Integrations**
3. Click the three dots in the top right
4. Select **Custom repositories**
5. Add repository URL: `https://github.com/jrigling/homeassistant-jebao`
6. Category: **Integration**
7. Click **Add**
8. Search for **Jebao** and install
9. Restart Home Assistant

### Manual Installation

1. Copy the `custom_components/jebao` directory to your Home Assistant
   `config/custom_components` directory
2. Restart Home Assistant

---

## Configuration

After install, go to **Settings → Devices & Services → + Add Integration
→ Jebao**. You'll be asked which pump you're adding:

- **MDP-20000 wavemaker (local network)**
- **MD-4.4 dosing pump (Gizwits cloud)**

### MDP-20000 setup

The wavemaker flow has two paths:

**Automatic discovery (recommended)**

1. Pick the **MDP-20000 wavemaker** option in the menu
2. Choose **Automatic discovery**
3. Select which network interfaces to scan (default: all)
4. Pick your pump from the discovered devices

**Manual**

If discovery doesn't reach your pump (different VLAN with no broadcast
forwarding, etc.) you can enter its IP directly. The pump must be on a
subnet your Home Assistant host can reach over TCP/12416.

### MD-4.4 setup

The doser flow takes your Gizwits cloud credentials:

1. Pick the **MD-4.4 dosing pump** option in the menu
2. Enter the **email**, **password** and **region** you use with the
   official Jebao Aqua app. Region is usually `us`; `eu` and `cn` are
   also supported.
3. The integration logs in, fetches the list of dosers on your account,
   and you pick which one to add.

Credentials are stored encrypted in the config entry. The integration
polls `/app/devdata/<did>/latest` every 30 s by default, and writes go
through `/app/control/<did>`.

> ⚠ The MD-4.4 needs internet to reach `usapi.gizwits.com` (or your
> region's equivalent). If your dosing pump is on a fully air-gapped
> IoT VLAN, this integration won't help — the pump itself needs to
> reach the Gizwits cloud for any control to work, official app
> included.

#### Multi-subnet (MDP-20000)

If your HA server has multiple interfaces (e.g. main + IoT VLAN), the
discovery step lets you pick which interfaces to broadcast on. Standard
discovery only goes out the default route; with this you can sweep an
isolated IoT subnet directly.

Example:

```
Home Assistant: 192.168.1.50
  - eth0: 192.168.1.0/24 (main network)
  - eth1: 10.20.20.0/24 (IoT VLAN, pumps live here)

Jebao MDP-20000: 10.20.20.13
```

During discovery, select `eth1` (or both) to broadcast onto the IoT VLAN.

---

## Entities

### MDP-20000 entities

| Entity ID | Type | What it does |
|---|---|---|
| `fan.jebao_pump` | Fan | Primary on/off + speed control (30–100 %) |
| `binary_sensor.jebao_feed_mode` | Binary sensor | On while pump is paused for feeding |
| `button.jebao_start_feed` | Button | Pause pump for the configured feed duration |
| `button.jebao_cancel_feed` | Button | Resume immediately |
| `number.jebao_feed_duration` | Number | Feed-pause duration (1–10 min) |
| `sensor.jebao_speed` | Sensor | Current speed % |
| `sensor.jebao_state` | Sensor | Device state (OFF/ON/FEED/PROGRAM) |

### MD-4.4 entities

Per doser, the integration exposes:

#### Switches

| Entity | What it does |
|---|---|
| `switch.…_master` | Master power for the pump |
| `switch.…_channel_1` … `_channel_4` | Manual on/off per dosing channel |
| `switch.…_timer_1` … `_timer_4` | Enable/disable the stored schedule for that channel |
| `switch.…_cal_factor_10x` | 10× dose precision mode (Configuration section) — see below |

#### Numbers

| Entity | What it does |
|---|---|
| `number.…_channel_N_interval` | Days to **skip** between scheduled doses for channel N. `0` = every day, `1` = every other day, etc. |
| `number.…_actual_calibration_amount` | Calibration helper input — the real mL you want to dose |

#### Text (editable schedules)

| Entity | What it does |
|---|---|
| `text.…_channel_N_schedule` | Channel N's full schedule as comma-separated `HH:MM=mL` entries (up to 24). Edit it directly to rewrite the whole channel. |

The schedule text entity also exposes structured attributes — see
[Schedule attributes](#schedule-attributes) below.

#### Sensors

| Entity | What it does |
|---|---|
| `sensor.…_channel_N_schedules` | Count of programmed entries on channel N |
| `sensor.…_channel_N_next_dose` | Next upcoming dose as `HH:MM (mL)` |
| `sensor.…_calibration_channel` | Which channel is armed for calibration (1–4) |
| `sensor.…_calibration_value` | Stored calibration value |
| `sensor.…_device_clock` | The pump's internal clock |
| `sensor.…_calibration_amount_to_enter_in_app` | Computed value to type into the Jebao app — paired with the calibration amount input |

#### Binary sensors

| Entity | What it does |
|---|---|
| `binary_sensor.…_open_circuit` | A dosing motor lost drive |
| `binary_sensor.…_uart_fault` | MCU↔WiFi UART fault |

#### Buttons

| Entity | What it does |
|---|---|
| `button.…_sync_clock` | Sync the local wall clock to the pump (the pump's schedules fire against its own clock — sync after a power outage) |

---

## Services / Actions

### MD-4.4 schedule services

All five services accept the doser as a **device picker** in the
Developer Tools UI — pick which MD-4.4 you want to act on from the
dropdown. Examples below use `!device` so the YAML reads cleanly,
but in practice the UI fills this in for you.

#### `jebao.set_schedule`

Overwrite the whole schedule for one channel.

```yaml
service: jebao.set_schedule
data:
  device_id: !device 9Y4EeBqQuIfwfqR8XvvrRT
  channel: 2
  entries:
    - hour: 9
      minute: 0
      quantity_ml: 0.5
    - hour: 21
      minute: 0
      quantity_ml: 0.5
```

#### `jebao.set_schedule_slot`

Edit / add one entry. Slot numbering is 1-based and matches the order
shown in the text entity. Slots past the current count append a new
entry; `quantity_ml: 0` deletes the slot.

```yaml
service: jebao.set_schedule_slot
data:
  device_id: !device 9Y4EeBqQuIfwfqR8XvvrRT
  channel: 2
  slot: 1
  hour: 14
  minute: 30
  quantity_ml: 1.2
```

#### `jebao.delete_schedule_slot`

Explicit delete, in case you'd rather not overload `set_schedule_slot`
with a zero quantity.

```yaml
service: jebao.delete_schedule_slot
data:
  device_id: !device 9Y4EeBqQuIfwfqR8XvvrRT
  channel: 2
  slot: 2
```

#### `jebao.get_schedule` (returns response)

Read the whole channel into a script variable.

```yaml
service: jebao.get_schedule
data:
  device_id: !device 9Y4EeBqQuIfwfqR8XvvrRT
  channel: 2
response_variable: schedule
```

`schedule` then holds:

```yaml
channel: 2
factor: 10
entry_count: 2
entries:
  - { slot: 1, time: "12:00", hour: 12, minute: 0, quantity_ml: 0.5 }
  - { slot: 2, time: "14:30", hour: 14, minute: 30, quantity_ml: 0.7 }
```

#### `jebao.get_schedule_slot` (returns response)

Read one specific slot. If the slot is past the current count,
`exists: false` is returned so automations can branch cleanly.

```yaml
service: jebao.get_schedule_slot
data:
  device_id: !device 9Y4EeBqQuIfwfqR8XvvrRT
  channel: 2
  slot: 1
response_variable: dose
```

`dose` looks like:

```yaml
channel: 2
slot: 1
time: "12:00"
hour: 12
minute: 0
quantity_ml: 0.5
exists: true
```

### Quantity scaling — the 10× precision mode

When the **10× dose precision** switch is on, both `quantity_ml`
inputs and the values surfaced in responses/attributes are **real mL**
— the integration multiplies by 10 when writing to the cloud and
divides on read. With the switch off, everything is whole-mL (the
firmware's native unit).

The paired **Actual calibration amount** number entity and the
**Calibration amount to enter in app** sensor make the math easy to
do by eye too — type the real mL you want into the input, read the
integer you'd need to type into the official app (when you're not
using the schedule text entity).

---

## Schedule attributes

Each `text.…_channel_N_schedule` exposes structured data alongside
its string state, so you can pin individual slots to a dashboard
without parsing the text.

```yaml
state: "12:00=0.5, 14:30=0.7"
attributes:
  entry_count: 2
  factor: 10
  entries:
    - { slot: 1, time: "12:00", hour: 12, minute: 0, quantity_ml: 0.5 }
    - { slot: 2, time: "14:30", hour: 14, minute: 30, quantity_ml: 0.7 }
  slot_1_time: "12:00"
  slot_1_ml: 0.5
  slot_2_time: "14:30"
  slot_2_ml: 0.7
```

`slot_1_*` through `slot_3_*` are flattened for the common case where
you only want to pin the first few slots to a card.

### Template examples

State:

```jinja
{{ states('text.jebao_md_4_4_channel_2_schedule') }}
```

Attribute:

```jinja
{{ state_attr('text.jebao_md_4_4_channel_2_schedule', 'slot_1_time') }}
{{ state_attr('text.jebao_md_4_4_channel_2_schedule', 'slot_1_ml') }}
```

Iterate the full structured list:

```jinja
{% set entries = state_attr('text.jebao_md_4_4_channel_2_schedule', 'entries') %}
{% for e in entries %}
  Slot {{ e.slot }}: {{ e.time }} = {{ e.quantity_ml }} mL
{% endfor %}
```

Find the next dose after now:

```jinja
{% set now_min = now().hour * 60 + now().minute %}
{% set entries = state_attr('text.jebao_md_4_4_channel_2_schedule', 'entries') | default([]) %}
{% set upcoming = entries | selectattr('hour') | rejectattr(
  'hour', 'lt', now().hour) | list %}
{{ upcoming[0] if upcoming else "next is tomorrow" }}
```

(The integration also ships a ready-made `sensor.…_channel_N_next_dose`
that does this for you.)

---

## Usage examples

### MDP-20000 — basic control

```yaml
# Turn on at 75 %
service: fan.turn_on
target:
  entity_id: fan.jebao_pump
data:
  percentage: 75

# Turn off
service: fan.turn_off
target:
  entity_id: fan.jebao_pump
```

### MDP-20000 — feed mode

```yaml
# Set the pause duration
service: number.set_value
target:
  entity_id: number.jebao_feed_duration
data:
  value: 2  # 2 minutes

# Start the pause
service: button.press
target:
  entity_id: button.jebao_start_feed

# Cancel early
service: button.press
target:
  entity_id: button.jebao_cancel_feed
```

### MDP-20000 — automation: daily feed

```yaml
automation:
  - alias: Daily aquarium feeding
    trigger:
      - platform: time
        at: "08:00:00"
      - platform: time
        at: "18:00:00"
    action:
      - service: button.press
        target:
          entity_id: button.jebao_start_feed
```

### MDP-20000 — automation: night flow

```yaml
automation:
  - alias: Aquarium night mode
    trigger:
      - platform: time
        at: "22:00:00"
    condition:
      - condition: state
        entity_id: fan.jebao_pump
        state: "on"
    action:
      - service: fan.set_percentage
        target:
          entity_id: fan.jebao_pump
        data:
          percentage: 30
```

### MD-4.4 — increase tomorrow's morning dose conditionally

```yaml
automation:
  - alias: Bump CH2 dose if pH high
    trigger:
      - platform: numeric_state
        entity_id: sensor.aquarium_ph
        above: 8.4
        for: "01:00:00"
    action:
      - service: jebao.set_schedule_slot
        data:
          device_id: !device 9Y4EeBqQuIfwfqR8XvvrRT
          channel: 2
          slot: 1
          hour: "{{ state_attr('text.jebao_md_4_4_channel_2_schedule', 'slot_1_time').split(':')[0] | int }}"
          minute: "{{ state_attr('text.jebao_md_4_4_channel_2_schedule', 'slot_1_time').split(':')[1] | int }}"
          quantity_ml: "{{ (state_attr('text.jebao_md_4_4_channel_2_schedule', 'slot_1_ml') | float) + 0.1 }}"
```

### MD-4.4 — read before write

```yaml
script:
  doser_round_trip:
    sequence:
      - service: jebao.get_schedule_slot
        data:
          device_id: !device 9Y4EeBqQuIfwfqR8XvvrRT
          channel: 2
          slot: 1
        response_variable: dose
      - if: "{{ dose.exists }}"
        then:
          - service: notify.mobile_app
            data:
              message: "Channel 2 slot 1: {{ dose.time }} = {{ dose.quantity_ml }} mL"
```

---

## Lovelace cards

### MDP-20000 — control card

```yaml
type: entities
title: Aquarium Pump
entities:
  - entity: fan.jebao_pump
  - entity: sensor.jebao_speed
  - entity: binary_sensor.jebao_feed_mode
  - entity: button.jebao_start_feed
  - entity: button.jebao_cancel_feed
```

### MD-4.4 — channel schedule card

```yaml
type: entities
title: Channel 2 schedule
entities:
  - entity: switch.jebao_md_4_4_master
    name: Master
  - entity: switch.jebao_md_4_4_channel_2
    name: Channel 2 power
  - entity: switch.jebao_md_4_4_timer_2
    name: Timer 2 enabled
  - entity: text.jebao_md_4_4_channel_2_schedule
    name: Full schedule
  - entity: text.jebao_md_4_4_channel_2_schedule
    attribute: slot_1_time
    name: Dose 1 time
  - entity: text.jebao_md_4_4_channel_2_schedule
    attribute: slot_1_ml
    name: Dose 1 mL
  - entity: sensor.jebao_md_4_4_channel_2_next_dose
    name: Next dose
```

### MD-4.4 — calibration helper

```yaml
type: entities
title: Calibration helper
entities:
  - entity: switch.jebao_md_4_4_cal_factor_10x
  - entity: number.jebao_md_4_4_actual_calibration_amount
  - entity: sensor.jebao_md_4_4_calibration_amount_to_enter_in_app
```

---

## Configuration options

After adding the integration, go to **Settings → Devices & Services →
Jebao → Configure** to adjust the **scan_interval** (10–300 s,
default 30). Lower values are more responsive but produce more cloud
or LAN traffic.

---

## Troubleshooting

### MDP-20000 — discovery fails

1. Ensure pump is powered on and on the network
2. Check pump has a stable IP (DHCP reservation recommended)
3. Try manual configuration with the IP directly
4. Check firewall allows UDP/12414 broadcast
5. If you have multiple interfaces, make sure you selected the right one

### MDP-20000 — cannot connect

1. Verify the IP
2. Check TCP/12416 is reachable from HA
3. Close the official app — only one TCP client at a time
4. Power-cycle the pump if it's hung
5. Make sure HA can reach the pump's subnet

### MDP-20000 — pump becomes unavailable

1. Check network reachability
2. Confirm the IP hasn't changed (use DHCP reservation)
3. Look at the HA log
4. Reload the integration from **Devices & Services**
5. Power-cycle the pump

### MDP-20000 — Program mode

The MDP-20000 firmware has its own scheduling ("Program mode"). The
integration automatically exits Program mode on startup so manual HA
control works. Your stored schedule isn't deleted, just not active —
you can re-enable from the official app any time. **Best practice is
to use HA automations instead** for better integration.

### MD-4.4 — invalid auth

The credentials you entered didn't pass the Gizwits login. Double-check
the email and password match what you use in the official Jebao Aqua
app, and that you picked the correct region (US accounts → `us`).

### MD-4.4 — control commands work but UI is slow

The cloud's `/latest` cache can lag the pump's actual state by 2–5 s.
The integration uses optimistic state for switches and schedule
writes, so the UI updates immediately — but if you're watching the
raw cloud response (e.g. via REST debug logs), expect that brief lag.

### MD-4.4 — switches flap after a write

Older releases had a fixed 3-second verify delay that was too short.
Current versions hold the optimistic state through three retry windows
(5 s + 4 s + 4 s) until the cloud confirms. If you still see flapping
after pulling the latest version, send a log with `custom_components.jebao`
at `debug` level.

### MD-4.4 — schedule doesn't appear in dashboard immediately after a
service call

You're probably on an older version — pull at least `0.5.3`, which
rebinds the coordinator's state pointer after a write so the schedule
text entity (and all dependent sensors) re-renders inside the same tick.

---

## Dependencies

- `python-jebao` — underlying library for the MDP-20000 protocol
- `netifaces` — multi-interface network discovery
- `aiohttp` — bundled with Home Assistant; used for the MD-4.4 cloud
  client

All three install automatically.

---

## Support

- **Issues:** [GitHub Issues](https://github.com/jrigling/homeassistant-jebao/issues)
- **Discussions:** [GitHub Discussions](https://github.com/jrigling/homeassistant-jebao/discussions)
- **MDP-20000 protocol docs:** [python-jebao](https://github.com/jrigling/python-jebao)
- **MD-4.4 reverse-engineering reference:**
  [tancou/jebao-dosing-pump-md-4.4](https://github.com/tancou/jebao-dosing-pump-md-4.4)

## Credits

- [@tancou](https://github.com/tancou) — the original Node.js MD-4.4
  reverse-engineering work that documented the GizWits LAN frame format
  (the on-wire structure ended up not being usable for writes on
  current firmware, but reads still use it conceptually)
- MDP-20000 protocol reverse-engineered through packet captures of the
  official Jebao Aqua mobile app
- MD-4.4 cloud API extracted from the decompiled Jebao Aqua APK (app id
  `c3703c4888ec4736a3a0d9425c321604`, US region endpoint
  `usapi.gizwits.com`)

Thanks to the HA community for testing and feedback.

## License

MIT License

## Disclaimer

This is an unofficial integration not affiliated with Jebao or Gizwits.
Use at your own risk. Device warranty may be affected by third-party
control software. Cloud control for the MD-4.4 routes through Gizwits's
infrastructure — make sure that's acceptable for your setup before
adding the integration.
