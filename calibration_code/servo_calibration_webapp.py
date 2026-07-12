"""
Servo Calibration Web App
==========================

Runs on a Raspberry Pi Pico 2 W. Connects to WiFi and serves a two-tab
web page for calibrating the self-playing ukulele's fret-hand servos,
each string having a "finger" (fret-select) servo on channels 0-3 and
a "lift" (press/raise) servo on channels 4-7 of a PCA9685
(string N uses channel N-1 for finger, channel N-1+4 for lift).

Tab 1 - Servo Center Calibration
---------------------------------
Jog each PCA9685 channel with a slider to find its true center/zero
position, flag it as inverted, set travel limits, and save the result
to "servo_config.csv". Each channel has its own ON/OFF switch; channels
start OFF at boot so nothing is powered until you flip it on.

Tab 2 - Fret Position Calibration
-----------------------------------
Pick a string (1-4); its finger and lift servos are the only ones
powered. Use the arrow keys to jog into position: Left/Right jog the
finger (fret-select) servo, Up/Down jog the lift servo. Right and Up
always mean "increase logical angle" and Left/Down mean "decrease
logical angle" -- if a channel is flagged Inverted on the Center tab,
the physical direction is flipped to match, and jogging is clamped to
that channel's min/max travel limits from the Center tab so you can
never drive a servo past its set range. Once a finger/lift pair is
positioned over a fret, press Save under that fret number (1-5) to
record the pair's current angles; Clear removes a saved position. These
are stored in "fret_positions.json", separate from the raw hardware
calibration in servo_config.csv.

Usage
-----
1. Fill in WIFI_SSID / WIFI_PASSWORD below.
2. Run this script on the Pico. It will print the IP address to
   connect to once WiFi is up.
3. Open that IP address in a browser on the same network.
"""

from machine import Pin, I2C
import network
import socket
import time
import json

# ---------------------------------------------------------------------------
# WiFi
# ---------------------------------------------------------------------------
WIFI_SSID = "YOUR_WIFI_SSID"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"
WIFI_CONNECT_TIMEOUT_S = 20

# ---------------------------------------------------------------------------
# I2C / PCA9685
# ---------------------------------------------------------------------------
I2C_ID = 0
I2C_SDA_PIN = 0
I2C_SCL_PIN = 1
I2C_FREQ_HZ = 400000

PCA9685_ADDRESS = 0x40
NUM_CHANNELS = 16            # full width of the PCA9685; unused rows just sit at defaults

SERVO_FREQ_HZ = 50
SERVO_MIN_US = 500           # tune per servo datasheet
SERVO_MAX_US = 2500          # tune per servo datasheet
ANGLE_MIN = 0
ANGLE_MAX = 180
DEFAULT_ANGLE = 90.0

CONFIG_FILE = "servo_config.csv"
HTTP_PORT = 80

# ---------------------------------------------------------------------------
# Fret-hand layout
# ---------------------------------------------------------------------------
NUM_STRINGS = 4               # strings 1-4
NUM_FRETS = 5                 # saved fret positions 1-5 per string
FRET_POSITIONS_FILE = "fret_positions.json"

i2c = I2C(I2C_ID, sda=Pin(I2C_SDA_PIN), scl=Pin(I2C_SCL_PIN), freq=I2C_FREQ_HZ)


def finger_channel(string_num):
    return string_num - 1


def lift_channel(string_num):
    return string_num - 1 + NUM_STRINGS


# ---------------------------------------------------------------------------
# PCA9685 driver
#
# MODE1/PRESCALE sequence follows Adafruit's reference MicroPython
# driver: https://github.com/adafruit/micropython-adafruit-pca9685/blob/master/pca9685.py
# The full-off bit (bit 4 of each channel's OFF_H register) is documented
# in the NXP PCA9685 datasheet as the fast, per-channel shutdown method:
# https://cdn-shop.adafruit.com/datasheets/PCA9685.pdf
# ---------------------------------------------------------------------------
class PCA9685:
    MODE1 = 0x00
    PRESCALE = 0xFE
    LED0_ON_L = 0x06
    FULL_OFF_BIT = 0x10

    def __init__(self, i2c_bus, address=PCA9685_ADDRESS):
        self.i2c = i2c_bus
        self.address = address
        self.reset()

    def _write(self, reg, value):
        self.i2c.writeto_mem(self.address, reg, bytearray([value]))

    def _read(self, reg):
        return self.i2c.readfrom_mem(self.address, reg, 1)[0]

    def reset(self):
        self._write(self.MODE1, 0x00)

    def set_freq(self, freq_hz):
        prescale = int(25000000.0 / 4096.0 / freq_hz + 0.5)
        old_mode = self._read(self.MODE1)
        self._write(self.MODE1, (old_mode & 0x7F) | 0x10)   # sleep so PRESCALE can be written
        self._write(self.PRESCALE, prescale)
        self._write(self.MODE1, old_mode)
        time.sleep_us(5000)
        self._write(self.MODE1, old_mode | 0xA1)            # wake, auto-increment on

    def set_pwm(self, channel, on, off):
        reg = self.LED0_ON_L + 4 * channel
        self._write(reg, on & 0xFF)
        self._write(reg + 1, on >> 8)
        self._write(reg + 2, off & 0xFF)
        self._write(reg + 3, off >> 8)

    def set_duty(self, channel, duty_12bit):
        # Writing a normal (< 4096) OFF value always leaves bit 4 of
        # OFF_H clear, so this also cancels any previous full-off.
        self.set_pwm(channel, 0, duty_12bit)

    def set_full_off(self, channel):
        off_h_reg = self.LED0_ON_L + 4 * channel + 3
        self._write(off_h_reg, self.FULL_OFF_BIT)


pca = PCA9685(i2c)
pca.set_freq(SERVO_FREQ_HZ)


def angle_to_duty(angle):
    angle = max(ANGLE_MIN, min(ANGLE_MAX, angle))
    pulse_us = SERVO_MIN_US + (SERVO_MAX_US - SERVO_MIN_US) * (angle - ANGLE_MIN) / (ANGLE_MAX - ANGLE_MIN)
    period_us = 1000000 / SERVO_FREQ_HZ
    return int(pulse_us * 4096 / period_us)


# ---------------------------------------------------------------------------
# Per-channel state
# ---------------------------------------------------------------------------
# state[i] = {"center": float, "inverted": bool, "enabled": bool, "angle": float,
#             "min_angle": float or None, "max_angle": float or None}
# min_angle/max_angle are the travel limits set from the web app; they're
# persisted to servo_config.csv alongside center/inverted.
state = [
    {
        "center": DEFAULT_ANGLE,
        "inverted": False,
        "enabled": False,
        "angle": DEFAULT_ANGLE,
        "min_angle": None,
        "max_angle": None,
    }
    for _ in range(NUM_CHANNELS)
]

# fret_positions["<string>"]["<fret>"] = {"finger": angle, "lift": angle}
fret_positions = {}


def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            for i, line in enumerate(f):
                if i >= NUM_CHANNELS:
                    break
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                center_str = parts[0]
                inverted_str = parts[1] if len(parts) > 1 else "0"
                min_str = parts[2].strip() if len(parts) > 2 else ""
                max_str = parts[3].strip() if len(parts) > 3 else ""

                state[i]["center"] = float(center_str)
                state[i]["inverted"] = inverted_str.strip() == "1"
                state[i]["min_angle"] = float(min_str) if min_str != "" else None
                state[i]["max_angle"] = float(max_str) if max_str != "" else None
                state[i]["angle"] = clamp_to_limits(i, float(center_str))
        print("Loaded existing {}.".format(CONFIG_FILE))
    except OSError:
        print("No {} found yet -- starting from defaults.".format(CONFIG_FILE))


def save_config():
    with open(CONFIG_FILE, "w") as f:
        for ch in state:
            min_str = "" if ch["min_angle"] is None else str(ch["min_angle"])
            max_str = "" if ch["max_angle"] is None else str(ch["max_angle"])
            f.write("{},{},{},{}\n".format(ch["center"], 1 if ch["inverted"] else 0, min_str, max_str))
    print("Saved {}.".format(CONFIG_FILE))


def load_frets():
    global fret_positions
    try:
        with open(FRET_POSITIONS_FILE, "r") as f:
            fret_positions = json.load(f)
        print("Loaded existing {}.".format(FRET_POSITIONS_FILE))
    except (OSError, ValueError):
        fret_positions = {}
        print("No {} found yet -- starting from defaults.".format(FRET_POSITIONS_FILE))


def save_frets():
    with open(FRET_POSITIONS_FILE, "w") as f:
        json.dump(fret_positions, f)
    print("Saved {}.".format(FRET_POSITIONS_FILE))


def save_fret_position(string_num, fret_num):
    key_s = str(string_num)
    key_f = str(fret_num)
    if key_s not in fret_positions:
        fret_positions[key_s] = {}
    fret_positions[key_s][key_f] = {
        "finger": state[finger_channel(string_num)]["angle"],
        "lift": state[lift_channel(string_num)]["angle"],
    }
    save_frets()


def clear_fret_position(string_num, fret_num):
    key_s = str(string_num)
    key_f = str(fret_num)
    if key_s in fret_positions and key_f in fret_positions[key_s]:
        del fret_positions[key_s][key_f]
        save_frets()


def all_channels_off():
    for channel in range(NUM_CHANNELS):
        pca.set_full_off(channel)


def clamp_to_limits(channel, angle):
    ch = state[channel]
    low = ch["min_angle"] if ch["min_angle"] is not None else ANGLE_MIN
    high = ch["max_angle"] if ch["max_angle"] is not None else ANGLE_MAX
    return max(low, min(high, angle))


def apply_angle(channel, angle):
    ch = state[channel]
    angle = clamp_to_limits(channel, angle)
    ch["angle"] = angle
    if ch["enabled"]:
        pca.set_duty(channel, angle_to_duty(angle))


def set_limit(channel, which, action):
    """which is 'min' or 'max'; action is 'set' (use the current angle) or 'clear'."""
    key = "min_angle" if which == "min" else "max_angle"
    if action == "set":
        state[channel][key] = state[channel]["angle"]
    else:
        state[channel][key] = None
    save_config()   # persist the limit immediately so it survives a power cycle


def set_enabled(channel, enabled):
    state[channel]["enabled"] = enabled
    if enabled:
        pca.set_duty(channel, angle_to_duty(state[channel]["angle"]))
    else:
        pca.set_full_off(channel)


def select_string(string_num):
    """Power only the chosen string's finger + lift servos; everything
    else on the fret hand goes off, same one-pair-at-a-time safety
    behaviour as the per-channel switches on the Center tab."""
    fc = finger_channel(string_num)
    lc = lift_channel(string_num)
    for ch in range(NUM_STRINGS * 2):
        set_enabled(ch, ch == fc or ch == lc)


# ---------------------------------------------------------------------------
# WiFi
# ---------------------------------------------------------------------------
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    print("Connecting to WiFi", end="")
    start = time.time()
    while not wlan.isconnected():
        if time.time() - start > WIFI_CONNECT_TIMEOUT_S:
            raise RuntimeError("Could not connect to WiFi -- check WIFI_SSID / WIFI_PASSWORD")
        print(".", end="")
        time.sleep(0.5)
    print()
    ip = wlan.ifconfig()[0]
    print("Connected. Open http://{} in a browser on the same network.".format(ip))
    return wlan


# ---------------------------------------------------------------------------
# Web page
# ---------------------------------------------------------------------------
INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ukulele Servo Calibration</title>
<style>
  body { font-family: sans-serif; background:#111; color:#eee; margin:0; padding:16px; }
  h1 { font-size:1.2em; }
  h2 { font-size:1.05em; margin-top:20px; }
  p { color:#bbb; font-size:0.9em; }
  .tabs { display:flex; gap:8px; margin-bottom:16px; }
  .tab-btn { background:#222; color:#ccc; border:1px solid #444; padding:8px 14px; border-radius:6px 6px 0 0; font-size:0.95em; cursor:pointer; }
  .tab-btn.active { background:#2e7d32; color:white; border-color:#2e7d32; }
  .channel-block { padding:10px 0; border-bottom:1px solid #333; }
  .row { display:flex; align-items:center; gap:12px; }
  .row span.ch { width:28px; font-weight:bold; }
  input[type=range] { flex:1; }
  .angle { width:48px; text-align:right; }
  .switch { position:relative; display:inline-block; width:46px; height:24px; flex-shrink:0; }
  .switch input { opacity:0; width:0; height:0; }
  .slider-toggle { position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background-color:#555; transition:.2s; border-radius:24px; }
  .slider-toggle:before { position:absolute; content:""; height:18px; width:18px; left:3px; bottom:3px; background-color:white; transition:.2s; border-radius:50%; }
  input:checked + .slider-toggle { background-color:#2e7d32; }
  input:checked + .slider-toggle:before { transform:translateX(22px); }
  button { background:#2e7d32; color:white; border:none; padding:10px 18px; border-radius:6px; font-size:1em; cursor:pointer; margin-top:12px; }
  button:active { background:#1b5e20; }
  label.inv { font-size:0.85em; color:#ccc; display:flex; align-items:center; gap:4px; flex-shrink:0; }
  .limits { display:flex; align-items:center; gap:6px; margin:6px 0 0 40px; }
  .limits span { font-size:0.85em; color:#ccc; }
  .limit-box { width:48px; background:#222; color:#eee; border:1px solid #444; border-radius:4px; text-align:center; padding:4px 2px; font-size:0.85em; }
  .limits button { padding:4px 10px; font-size:0.8em; margin-top:0; }
  .limits button.clear-btn { background:#555; }
  .limits button.clear-btn:active { background:#3a3a3a; }
  .string-select { display:flex; gap:8px; margin:12px 0; }
  .string-btn { width:44px; height:44px; font-size:1.1em; background:#222; color:#eee; border:1px solid #444; border-radius:6px; cursor:pointer; margin-top:0; }
  .string-btn.active { background:#2e7d32; border-color:#2e7d32; }
  .live-readout { background:#1a1a1a; border:1px solid #333; border-radius:6px; padding:12px; margin:12px 0; }
  .live-readout div { margin:4px 0; }
  .hint { margin:8px 0 0 0; }
  .fret-block { background:#1a1a1a; border:1px solid #333; border-radius:6px; padding:12px; margin:10px 0; }
  .fret-block h3 { margin:0 0 8px 0; font-size:1em; }
  .fret-vals { display:flex; gap:16px; margin-bottom:8px; color:#ccc; font-size:0.9em; }
  .fret-actions { display:flex; gap:8px; }
  .fret-actions button { margin-top:0; }
  .fret-actions button.clear-btn { background:#555; }
  .fret-actions button.clear-btn:active { background:#3a3a3a; }
</style>
</head>
<body>

<div class="tabs">
  <button id="tabBtnCenter" class="tab-btn active" onclick="showTab('center')">Servo Center Calibration</button>
  <button id="tabBtnFret" class="tab-btn" onclick="showTab('fret')">Fret Position Calibration</button>
</div>

<div id="tabCenter">
  <h1>Servo Center Calibration</h1>
  <p>Turn a channel on, drag its slider until the horn sits where you want the logical zero to be,
     flip Inverted if it should count the other direction, then Save. Use Set Min / Set Max to lock
     the slider at the current angle so it can't be dragged past that point -- Clear removes the limit.</p>
  <div id="rows">Loading...</div>
  <button onclick="saveAll()">Save to servo_config.csv</button>
  <p id="status"></p>
</div>

<div id="tabFret" style="display:none">
  <h1>Fret Position Calibration</h1>
  <p>Pick a string below -- only that string's finger and lift servos are powered.
     Use &larr; / &rarr; to jog the finger (fret-select) servo and &uarr; / &darr; to jog the lift
     servo; both respect Inverted and the min/max travel limits set on the Center tab. Once the
     fingertip is positioned correctly, press Save under the fret number to record it.</p>
  <div class="string-select" id="stringSelect"></div>
  <div class="live-readout">
    <div>Finger (fret-select) servo -- channel <span id="fingerCh"></span>: <span id="fingerAngle">--</span>&deg;</div>
    <div>Lift (press/raise) servo -- channel <span id="liftCh"></span>: <span id="liftAngle">--</span>&deg;</div>
  </div>
  <h2>Position for fret:</h2>
  <div id="fretSections"></div>
</div>

<script>
// ---------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------
let activeTab = 'center';

function showTab(name) {
  activeTab = name;
  document.getElementById('tabCenter').style.display = name === 'center' ? '' : 'none';
  document.getElementById('tabFret').style.display = name === 'fret' ? '' : 'none';
  document.getElementById('tabBtnCenter').classList.toggle('active', name === 'center');
  document.getElementById('tabBtnFret').classList.toggle('active', name === 'fret');
  stopJog();
  if (name === 'fret') {
    selectString(currentString);
  }
}

// ---------------------------------------------------------------------
// Tab 1: Servo Center Calibration
// ---------------------------------------------------------------------
async function loadState() {
  const res = await fetch('/api/state');
  const data = await res.json();
  const container = document.getElementById('rows');
  container.innerHTML = '';
  data.forEach(ch => {
    const sliderMin = ch.min_angle !== null ? ch.min_angle : 0;
    const sliderMax = ch.max_angle !== null ? ch.max_angle : 180;
    const minText = ch.min_angle !== null ? Math.round(ch.min_angle) : '';
    const maxText = ch.max_angle !== null ? Math.round(ch.max_angle) : '';

    const block = document.createElement('div');
    block.className = 'channel-block';
    block.innerHTML =
      '<div class="row">' +
        '<span class="ch">' + ch.channel + '</span>' +
        '<label class="switch">' +
          '<input type="checkbox" ' + (ch.enabled ? 'checked' : '') + ' onchange="setEnabled(' + ch.channel + ', this.checked)">' +
          '<span class="slider-toggle"></span>' +
        '</label>' +
        '<input type="range" min="' + sliderMin + '" max="' + sliderMax + '" step="1" value="' + ch.angle + '" ' +
          'oninput="onSlide(' + ch.channel + ', this.value); this.nextElementSibling.textContent=this.value + \\'\\u00b0\\'">' +
        '<span class="angle">' + ch.angle + '\\u00b0</span>' +
        '<label class="inv">' +
          '<input type="checkbox" ' + (ch.inverted ? 'checked' : '') + ' onchange="setInverted(' + ch.channel + ', this.checked)">' +
          'Inverted' +
        '</label>' +
      '</div>' +
      '<div class="limits">' +
        '<span>Min:</span>' +
        '<input type="text" class="limit-box" value="' + minText + '" readonly>' +
        '<button onclick="setLimit(' + ch.channel + ', \\'min\\', \\'set\\')">Set Min</button>' +
        '<button class="clear-btn" onclick="setLimit(' + ch.channel + ', \\'min\\', \\'clear\\')">Clear</button>' +
        '<span>Max:</span>' +
        '<input type="text" class="limit-box" value="' + maxText + '" readonly>' +
        '<button onclick="setLimit(' + ch.channel + ', \\'max\\', \\'set\\')">Set Max</button>' +
        '<button class="clear-btn" onclick="setLimit(' + ch.channel + ', \\'max\\', \\'clear\\')">Clear</button>' +
      '</div>';
    container.appendChild(block);
  });
}

let sendTimer = null;
function onSlide(channel, angle) {
  clearTimeout(sendTimer);
  sendTimer = setTimeout(() => setAngle(channel, angle), 80);
}

async function setAngle(channel, angle) {
  await fetch('/api/angle', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'channel=' + channel + '&angle=' + angle});
}

async function setEnabled(channel, enabled) {
  await fetch('/api/enabled', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'channel=' + channel + '&enabled=' + (enabled ? 1 : 0)});
}

async function setInverted(channel, inverted) {
  await fetch('/api/inverted', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'channel=' + channel + '&inverted=' + (inverted ? 1 : 0)});
}

async function setLimit(channel, which, action) {
  await fetch('/api/limit', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'channel=' + channel + '&which=' + which + '&action=' + action});
  loadState();
}

async function saveAll() {
  await fetch('/api/save', {method:'POST'});
  document.getElementById('status').textContent = 'Saved at ' + new Date().toLocaleTimeString();
}

// ---------------------------------------------------------------------
// Tab 2: Fret Position Calibration
// ---------------------------------------------------------------------
const NUM_STRINGS = 4;
const NUM_FRETS = 5;
const JOG_STEP_DEG = 1;
const JOG_INTERVAL_MS = 60;
const JOG_KEYS = {
  ArrowRight: {axis: 'finger', sign: 1},
  ArrowLeft:  {axis: 'finger', sign: -1},
  ArrowUp:    {axis: 'lift',   sign: 1},
  ArrowDown:  {axis: 'lift',   sign: -1},
};

let currentString = 1;
let fingerChannel = 0;
let liftChannel = NUM_STRINGS;
let chState = {};
let fretPositions = {};
let jogKey = null;
let jogTimer = null;

function buildStringButtons() {
  const container = document.getElementById('stringSelect');
  container.innerHTML = '';
  for (let s = 1; s <= NUM_STRINGS; s++) {
    const btn = document.createElement('button');
    btn.className = 'string-btn' + (s === currentString ? ' active' : '');
    btn.textContent = s;
    btn.dataset.string = s;
    btn.onclick = () => selectString(s);
    container.appendChild(btn);
  }
}

async function selectString(s) {
  stopJog();
  currentString = s;
  fingerChannel = s - 1;
  liftChannel = s - 1 + NUM_STRINGS;
  document.querySelectorAll('.string-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.string) === s);
  });
  await fetch('/api/select_string', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'string=' + s});
  await refreshChState();
  await refreshFrets();
}

async function refreshChState() {
  const res = await fetch('/api/state');
  const data = await res.json();
  chState = {};
  data.forEach(c => chState[c.channel] = c);
  updateLiveReadout();
}

function updateLiveReadout() {
  document.getElementById('fingerCh').textContent = fingerChannel;
  document.getElementById('liftCh').textContent = liftChannel;
  const fc = chState[fingerChannel];
  const lc = chState[liftChannel];
  document.getElementById('fingerAngle').textContent = fc ? Math.round(fc.angle * 10) / 10 : '--';
  document.getElementById('liftAngle').textContent = lc ? Math.round(lc.angle * 10) / 10 : '--';
}

async function refreshFrets() {
  const res = await fetch('/api/frets');
  fretPositions = await res.json();
  renderFretSections();
}

function renderFretSections() {
  const container = document.getElementById('fretSections');
  container.innerHTML = '';
  const stringData = fretPositions[String(currentString)] || {};
  for (let fret = 1; fret <= NUM_FRETS; fret++) {
    const pos = stringData[String(fret)];
    const fingerText = pos ? (Math.round(pos.finger * 10) / 10) + '\\u00b0' : 'not set';
    const liftText = pos ? (Math.round(pos.lift * 10) / 10) + '\\u00b0' : 'not set';
    const block = document.createElement('div');
    block.className = 'fret-block';
    block.innerHTML =
      '<h3>' + fret + '</h3>' +
      '<div class="fret-vals">' +
        '<span>finger: ' + fingerText + '</span>' +
        '<span>lift: ' + liftText + '</span>' +
      '</div>' +
      '<div class="fret-actions">' +
        '<button onclick="saveFret(' + fret + ')">Save</button>' +
        '<button class="clear-btn" onclick="clearFret(' + fret + ')">Clear</button>' +
      '</div>';
    container.appendChild(block);
  }
}

async function saveFret(fret) {
  await fetch('/api/frets/save', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'string=' + currentString + '&fret=' + fret});
  await refreshFrets();
}

async function clearFret(fret) {
  await fetch('/api/frets/clear', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'string=' + currentString + '&fret=' + fret});
  await refreshFrets();
}

function jogStep() {
  if (!jogKey) return;
  const spec = JOG_KEYS[jogKey];
  const channel = spec.axis === 'finger' ? fingerChannel : liftChannel;
  const ch = chState[channel];
  if (!ch) return;
  const dir = ch.inverted ? -spec.sign : spec.sign;
  setAngleFret(channel, ch.angle + dir * JOG_STEP_DEG);
}

async function setAngleFret(channel, angle) {
  const res = await fetch('/api/angle', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'channel=' + channel + '&angle=' + angle});
  const data = await res.json();
  data.forEach(c => chState[c.channel] = c);
  updateLiveReadout();
}

function startJog(key) {
  if (jogKey === key) return;
  stopJog();
  jogKey = key;
  jogStep();
  jogTimer = setInterval(jogStep, JOG_INTERVAL_MS);
}

function stopJog() {
  if (jogTimer) {
    clearInterval(jogTimer);
    jogTimer = null;
  }
  jogKey = null;
}

document.addEventListener('keydown', function(e) {
  if (activeTab !== 'fret') return;
  if (!(e.key in JOG_KEYS)) return;
  e.preventDefault();
  if (e.repeat) return;
  startJog(e.key);
});

document.addEventListener('keyup', function(e) {
  if (!(e.key in JOG_KEYS)) return;
  if (jogKey === e.key) stopJog();
});

window.addEventListener('blur', stopJog);

buildStringButtons();
loadState();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Minimal HTTP server
# ---------------------------------------------------------------------------
def parse_form(body):
    result = {}
    for pair in body.split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            result[key] = value
    return result


def state_json():
    return json.dumps([
        {
            "channel": i,
            "angle": ch["angle"],
            "center": ch["center"],
            "inverted": ch["inverted"],
            "enabled": ch["enabled"],
            "min_angle": ch["min_angle"],
            "max_angle": ch["max_angle"],
        }
        for i, ch in enumerate(state)
    ])


def send_response(cl, status, content_type, body):
    if isinstance(body, str):
        body = body.encode()
    status_text = "OK" if status == 200 else "Not Found"
    header = "HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n".format(
        status, status_text, content_type, len(body))
    cl.send(header.encode())
    cl.send(body)


def recv_request(cl):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = cl.recv(1024)
        if not chunk:
            break
        data += chunk
    return data


def handle_client(cl):
    request = recv_request(cl)
    if not request:
        return

    header_data, _, rest = request.partition(b"\r\n\r\n")
    lines = header_data.split(b"\r\n")
    method, path, _ = lines[0].decode().split(" ")

    headers = {}
    for line in lines[1:]:
        if b":" in line:
            key, value = line.split(b":", 1)
            headers[key.strip().lower().decode()] = value.strip().decode()

    body = rest
    content_length = int(headers.get("content-length", 0))
    while len(body) < content_length:
        chunk = cl.recv(1024)
        if not chunk:
            break
        body += chunk
    body = body.decode()

    if method == "GET" and path == "/":
        send_response(cl, 200, "text/html", INDEX_HTML)

    elif method == "GET" and path == "/api/state":
        send_response(cl, 200, "application/json", state_json())

    elif method == "POST" and path == "/api/angle":
        params = parse_form(body)
        apply_angle(int(params["channel"]), float(params["angle"]))
        send_response(cl, 200, "application/json", state_json())

    elif method == "POST" and path == "/api/enabled":
        params = parse_form(body)
        set_enabled(int(params["channel"]), params["enabled"] == "1")
        send_response(cl, 200, "application/json", state_json())

    elif method == "POST" and path == "/api/inverted":
        params = parse_form(body)
        state[int(params["channel"])]["inverted"] = params["inverted"] == "1"
        send_response(cl, 200, "application/json", state_json())

    elif method == "POST" and path == "/api/limit":
        params = parse_form(body)
        set_limit(int(params["channel"]), params["which"], params["action"])
        send_response(cl, 200, "application/json", state_json())

    elif method == "POST" and path == "/api/save":
        save_config()
        send_response(cl, 200, "application/json", '{"ok": true}')

    elif method == "POST" and path == "/api/select_string":
        params = parse_form(body)
        select_string(int(params["string"]))
        send_response(cl, 200, "application/json", state_json())

    elif method == "GET" and path == "/api/frets":
        send_response(cl, 200, "application/json", json.dumps(fret_positions))

    elif method == "POST" and path == "/api/frets/save":
        params = parse_form(body)
        save_fret_position(int(params["string"]), int(params["fret"]))
        send_response(cl, 200, "application/json", json.dumps(fret_positions))

    elif method == "POST" and path == "/api/frets/clear":
        params = parse_form(body)
        clear_fret_position(int(params["string"]), int(params["fret"]))
        send_response(cl, 200, "application/json", json.dumps(fret_positions))

    else:
        send_response(cl, 404, "text/plain", "Not found")


def run_server():
    addr = socket.getaddrinfo("0.0.0.0", HTTP_PORT)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(5)
    print("Server listening on port {}.".format(HTTP_PORT))

    while True:
        cl, remote_addr = s.accept()
        try:
            handle_client(cl)
        except Exception as e:
            print("Client error:", e)
        finally:
            cl.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    load_config()
    load_frets()
    all_channels_off()   # nothing is powered until you flip a switch or pick a string
    connect_wifi()
    run_server()


main()
