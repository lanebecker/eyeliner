# Hardware Guide — vinyl-now-playing

## Complete Parts List

All prices are approximate US retail as of mid-2026. RAM shortages have pushed Pi prices up
from their original MSRPs — shop around across Adafruit, PiShop, CanaKit, and Amazon.

| # | Part | Specific Model | Est. Price | Notes |
|---|------|----------------|-----------|-------|
| 1 | Raspberry Pi | Pi 4 Model B — 4GB | ~$80 | Sweet spot for this workload. 2GB works but 4GB gives comfortable headroom for audio processing + pygame. |
| 2 | 7" HDMI Display | Waveshare 7" HDMI LCD (H) with case | ~$70 | 1024×600 IPS, includes adjustable-tilt case. Plug-and-play on Pi OS. |
| 3 | USB Audio Interface | Behringer U-Control UCA222 | ~$30 | Stereo RCA inputs, USB bus-powered, zero-driver on Linux. The red one. |
| 4 | Power Supply (Pi) | Official Raspberry Pi 4 USB-C PSU (5V 3A) | ~$12 | Don't cheap out here — underpowered Pi = random crashes mid-record. |
| 5 | microSD Card | SanDisk 32GB Ultra A1 or Samsung Endurance 32GB | ~$11 | A1-rated cards handle the random I/O of Pi OS much better than generic cards. |
| 6 | Micro-HDMI Cable | Short micro-HDMI to HDMI (6"–1ft) | ~$9 | ⚠️ Pi 4 uses MICRO-HDMI, not mini-HDMI and not full HDMI. Easy to buy the wrong thing. |
| 7 | RCA Y-Splitters | Stereo RCA Y-splitter pair (1M → 2F each channel) | ~$7 | Only needed if your phono preamp has a single RCA output. See wiring section. |

**Estimated total: ~$219**  
(~$192 if you use the Pi 4 2GB at ~$53 instead)

---

## Your Phono Preamp: Cambridge Audio Alva Duo

The Alva Duo has a single pair of gold-plated RCA outputs — no second "tape out" or
record output. **You will need the RCA Y-splitters.**

This is totally fine: splitting a line-level signal (which is what the Alva Duo outputs)
is safe and transparent. The impedance math works out to less than 1% signal loss, and
you won't be able to hear the difference. The problem scenario — splitting a raw phono
signal *before* a preamp — doesn't apply here because the Alva Duo already handles
the phono-to-line conversion before the signal reaches your splitters.

---

## Signal Chain

```
┌─────────────┐    phono cable     ┌──────────────────┐
│  TURNTABLE  │ ─────────────────► │  PHONO PREAMP    │
│             │   (RCA, ~1m)       │                  │
└─────────────┘                    └────────┬─────────┘
                                            │
                                   line-level RCA output
                                            │
                          ┌─────────────────┤
                          │ (Y-splitters    │
                          │  if needed)     │
                          │                 │
               ┌──────────▼──────┐   ┌──────▼───────────────┐
               │  RECEIVER / AMP │   │  BEHRINGER UCA222     │
               │  (your existing │   │  (USB audio interface) │
               │   speaker setup)│   └──────────┬────────────┘
               └─────────────────┘              │
                                           USB-A to USB-B mini
                                                │
                                     ┌──────────▼──────────┐
                                     │   RASPBERRY PI 4    │
                                     │                     │
                                     │  USB-C ◄── PSU      │
                                     └──────────┬──────────┘
                                                │
                                          micro-HDMI
                                                │
                                     ┌──────────▼──────────┐
                                     │  WAVESHARE 7" LCD   │
                                     │  (HDMI + USB power) │
                                     └─────────────────────┘
```

---

## Step-by-Step Wiring

### Step 1 — Intercept the signal from your phono preamp

Your Alva Duo has a single RCA output pair, so you're using Y-splitters:

1. Unplug the RCA cables that currently run from the Alva Duo to your receiver.
2. Plug the Y-splitters onto the Alva Duo's output jacks (one Y per channel — left and right).
3. Run one leg of each Y back to your receiver exactly as before.
4. Run the other leg of each Y to the **INPUT** jacks on the UCA222.

Either way, your receiver continues to get the full signal and nothing in your listening
experience changes. The Pi is simply eavesdropping.

### Step 2 — Connect the UCA222 to the Raspberry Pi

Plug the UCA222's USB cable (USB-A to USB-B mini, usually included in the box) into any
of the Pi's four USB-A ports. It is bus-powered — no wall plug needed.

The UCA222 will appear as **"USB Audio Codec"** in sounddevice on Pi OS, which conveniently
matches the default `device_name` in `config.example.yaml`. ✓

### Step 3 — Connect the display

1. Plug the **micro-HDMI end** of your cable into the Pi's **HDMI0 port** (the one closest
   to the USB-C power port).
2. Plug the **HDMI end** into the display.
3. If your Waveshare (H) came with a short USB-A cable for the display's power input, plug
   that into one of the Pi's USB ports. The display can also take power from its own micro-USB
   port if you'd rather not use a Pi USB port for it.

> Note: The Waveshare (H) also has a USB input for touch. You don't need to connect it —
> this project doesn't use touch input. Just the HDMI cable is enough for the display to work.

### Step 4 — Power the Pi

Plug the official USB-C PSU into the Pi's USB-C power port and into the wall.
**Power the Pi last** — after everything else is connected — to ensure Pi OS enumerates
the audio interface correctly on boot.

---

## config.yaml Display Resolution

The `config.example.yaml` already defaults to **1024×600** to match the Waveshare 7" (H),
so a straight `cp config.example.yaml config.yaml` gives you the right values with no
manual edits needed. If you end up using a different display, this is the only section
you'd need to change.

---

## Verify the Audio Interface is Detected

After booting, SSH into the Pi (or open a terminal) and run:

```bash
python3 -c "import sounddevice; print(sounddevice.query_devices())"
```

You should see an entry like:

```
  2 USB Audio Codec: USB Audio (hw:2,0),
     hostapi: ALSA, max_input_channels: 2, ...
```

The exact index number will vary, but as long as "USB Audio Codec" appears with
`max_input_channels: 2`, you're good. That string matches the `device_name` in config.yaml.

---

## Verify the Display

The Waveshare display should just work on Pi OS with HDMI. If it comes up at the wrong
resolution, add these lines to `/boot/config.txt`:

```
hdmi_group=2
hdmi_mode=87
hdmi_cvt=1024 600 60 6 0 0 0
hdmi_drive=1
```

Then reboot.

---

## Physical Placement

A few practical notes on where to put things:

**The Pi + display** can sit on a shelf near your turntable. The Waveshare (H) case has a
tilt stand with 30°/50° positions, so you can angle it nicely without mounting anything.
Many people run a short HDMI extender and place the display on top of or beside the turntable
lid for a "now playing" panel effect.

**The UCA222** is tiny (smartphone-sized) and can tuck behind your preamp or receiver.
The USB cable runs to the Pi; the RCA cables run to the preamp. It doesn't need to be
visible.

**Cable tidiness:** The main annoyance is the RCA cable run from your preamp area to
wherever the Pi lives. If that's more than a meter or two, pick up a decent-quality
shielded RCA cable — generic cables over longer runs can pick up hum from power cables.
Route them away from power cables where possible.

---

## Total Power Draw

For reference, everything running simultaneously:
- Raspberry Pi 4 (loaded): ~6W
- Waveshare 7" display: ~2.5W
- Behringer UCA222: ~0.5W (bus-powered from Pi)
- **Total at the wall: ~9W**

A single 5V/3A (15W) PSU for the Pi handles this comfortably.

---

## Summary Shopping Links

Search these exact model names — prices vary by retailer:

- **Raspberry Pi 4 Model B 4GB** — [raspberrypi.com](https://www.raspberrypi.com/products/raspberry-pi-4-model-b/), Adafruit, PiShop, CanaKit
- **Waveshare 7inch HDMI LCD (H)** — [waveshare.com](https://www.waveshare.com/7inch-hdmi-lcd-h.htm), Amazon
- **Behringer U-Control UCA222** — Sweetwater, Amazon, B&H
- **Official Raspberry Pi 4 USB-C PSU** — anywhere Pi accessories are sold
- **SanDisk 32GB Ultra A1 microSD** — Amazon, Best Buy, Target
- **Micro-HDMI to HDMI cable (short)** — search "micro HDMI to HDMI 1ft" on Amazon; Ugreen and Anker both make reliable ones
- **RCA Y-splitters** — search "RCA Y-splitter male to 2 female" on Amazon; any passive pair works
