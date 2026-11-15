# FNAV-LEAD: a lead-acid battery monitor and charger switch (XIAO ESP32-C6)

The third power station of fallout.nav: a Seeed Studio **XIAO ESP32-C6** next to a 12 V lead-acid battery. It
measures the battery and the charger's 12 V supply, switches the charger on through an RC relay when 12 V is
present, and reports over **Bluetooth LE** as `FNAV-LEAD`. The Pi (`fnav/core/leadbatt.py`) reads it every 30 s and
shows the charge on the car screen and the web Dashboard; the Dashboard can also switch the charger by hand.

The full design notes (in Russian) are in [`docs/contracts/power.md`](../../docs/contracts/power.md), section
"Третья станция".

## Wiring

| XIAO pin | Goes to | Notes |
|---|---|---|
| 5V | DC-DC 12 → 5 V output, through a Schottky diode | so the DC-DC is not back-fed from the PC while flashing over USB |
| GND | common ground | battery −, charger input −, relay −, DC-DC − |
| A0 | battery + through a divider | 100 kΩ on top, 20 kΩ to GND, 100 nF across the 20 kΩ, close to the board |
| A1 | charger's 12 V input through a divider | same divider as A0 |
| D3 | RC relay switch, signal (S) | relay + → 5 V, − → GND |

- The divider brings 12–15 V down to 2.0–2.5 V for the ADC (11 dB attenuation, up to ~3.1 V). Any pair with a ratio
  of 4.5–6.5 works: set `DIVIDER` in the sketch to `(top + bottom) / bottom`.
- Calibrate against a multimeter: `CAL_BAT` and `CAL_IN` are `true voltage / shown voltage`.
- The relay is the same kind as the hazard-light relay on the Pi: 50 Hz servo pulses from the hardware PWM (LEDC),
  2000 µs closed, 1000 µs open. "Off" pulses keep going all the time.

## Behaviour

- **Auto (default):** 12 V on the input for 3 s (≥ 11.5 V) closes the relay and the charger runs; below 10.5 V for
  2 s opens it. The hysteresis and delays keep the relay from chattering.
- **Manual on / off** from the Dashboard's slide switches. The manual mode lasts until the module restarts, then it
  is back to auto.
- The charge percentage is estimated on the Pi from the resting voltage (11.31 V is 0 %, 12.73 V is 100 %). It reads
  high while charging.

## Bluetooth LE protocol

Service `6e4c0001-f4a5-4c3a-9f39-1d2b5f0c7a01`:

| Characteristic | UUID | Access |
|---|---|---|
| STATUS | `6e4c0002-f4a5-4c3a-9f39-1d2b5f0c7a01` | read, notify (every 2 s while connected) |
| CMD | `6e4c0003-f4a5-4c3a-9f39-1d2b5f0c7a01` | write, write without response |

**CMD** is one byte: `0x01` sends STATUS now, `0x10` is charger auto, `0x11` is charger on (manual), `0x12` is
charger off (manual). Any other byte only sends STATUS.

**STATUS** is 14 bytes, little-endian:

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | `'F' 'L'` |
| 2 | 1 | version (1) |
| 3 | 1 | flags: bit 0 is 12 V on the input, bit 1 is the relay (charger) on, bits 2–3 are the mode (0 auto, 1 on, 2 off) |
| 4 | 2 | battery voltage, mV |
| 6 | 2 | input voltage, mV |
| 8 | 4 | uptime, s |
| 12 | 2 | CRC16/Modbus of bytes 0–11 |

## Build and flash

Arduino core for ESP32 3.x (`esp32:esp32`):

```bash
arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32C6 --upload -p COM3 firmware/lead-battery
```

The serial monitor at 115200 prints the BLE address on start and a status line every 2 s. Opening the port with a
program that toggles DTR/RTS can leave the chip in the bootloader: press RESET.

The BLE address is the chip's base MAC + 2. Put it in the web UI under Bluetooth → Power stations → third station.
