"""
Fretboard Movement Library
===========================

Runs on a Raspberry Pi Pico 2 W. Drives the fret hand's 8 servos
(finger/fret-select on channels 0-3, lift/press on channels 4-7 --
string N uses channel N-1 for finger, channel N-1+4 for lift) from the
calibration data produced by calibration_code/servo_calibration_webapp.py:
"servo_config.csv" (per-channel center/inverted/min/max) and
"fret_positions.json" (per-string, per-fret finger+lift angles).

Usage
-----
    from movement import Fretboard

    fb = Fretboard()
    fb.move([2, 3, 2, 0])              # G chord, string 4 open
    fb.move([0, 2, 3, 2], [2, 1, 0, 0]) # look ahead to the next shape
"""

from machine import Pin, I2C
import time
import json

# ---------------------------------------------------------------------------
# I2C / PCA9685 -- must match the wiring used in calibration_code
# ---------------------------------------------------------------------------
I2C_ID = 0
I2C_SDA_PIN = 0
I2C_SCL_PIN = 1
I2C_FREQ_HZ = 400000

PCA9685_ADDRESS = 0x40
NUM_CHANNELS = 16

SERVO_FREQ_HZ = 50
SERVO_MIN_US = 500
SERVO_MAX_US = 2500
ANGLE_MIN = 0
ANGLE_MAX = 180
DEFAULT_ANGLE = 90.0

CONFIG_FILE = "servo_config.csv"
FRET_POSITIONS_FILE = "fret_positions.json"

# ---------------------------------------------------------------------------
# Fret-hand layout
# ---------------------------------------------------------------------------
NUM_STRINGS = 4
OPEN_STRING_DEFAULT_FRET = 3   # where to rest the finger servo on an open
                                # string when there's no next_state to look
                                # ahead to -- a middle fret minimizes worst
                                # case travel to wherever it's needed next

DEFAULT_LIFT_DELAY_MS = 50   # raise-all -> finger-move settle time
DEFAULT_PRESS_DELAY_MS = 30  # finger-move -> press-down settle time


def finger_channel(string_num):
    return string_num - 1


def lift_channel(string_num):
    return string_num - 1 + NUM_STRINGS


# ---------------------------------------------------------------------------
# PCA9685 driver (same as calibration_code/servo_calibration_webapp.py)
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
        self._write(self.MODE1, (old_mode & 0x7F) | 0x10)
        self._write(self.PRESCALE, prescale)
        self._write(self.MODE1, old_mode)
        time.sleep_us(5000)
        self._write(self.MODE1, old_mode | 0xA1)

    def set_pwm(self, channel, on, off):
        reg = self.LED0_ON_L + 4 * channel
        self._write(reg, on & 0xFF)
        self._write(reg + 1, on >> 8)
        self._write(reg + 2, off & 0xFF)
        self._write(reg + 3, off >> 8)

    def set_duty(self, channel, duty_12bit):
        self.set_pwm(channel, 0, duty_12bit)

    def set_full_off(self, channel):
        off_h_reg = self.LED0_ON_L + 4 * channel + 3
        self._write(off_h_reg, self.FULL_OFF_BIT)


def angle_to_duty(angle):
    angle = max(ANGLE_MIN, min(ANGLE_MAX, angle))
    pulse_us = SERVO_MIN_US + (SERVO_MAX_US - SERVO_MIN_US) * (angle - ANGLE_MIN) / (ANGLE_MAX - ANGLE_MIN)
    period_us = 1000000 / SERVO_FREQ_HZ
    return int(pulse_us * 4096 / period_us)


class Fretboard:
    def __init__(self, config_file=CONFIG_FILE, fret_positions_file=FRET_POSITIONS_FILE):
        self.config_file = config_file
        self.fret_positions_file = fret_positions_file

        self.channels = [
            {"inverted": False, "min_angle": None, "max_angle": None, "angle": DEFAULT_ANGLE}
            for _ in range(NUM_CHANNELS)
        ]
        self.fret_positions = {}

        self._load_config()
        self._load_fret_positions()

        i2c = I2C(I2C_ID, sda=Pin(I2C_SDA_PIN), scl=Pin(I2C_SCL_PIN), freq=I2C_FREQ_HZ)
        self.pca = PCA9685(i2c)
        self.pca.set_freq(SERVO_FREQ_HZ)

        self._raise_all()

    # -- setup ---------------------------------------------------------
    def _load_config(self):
        try:
            with open(self.config_file, "r") as f:
                for i, line in enumerate(f):
                    if i >= NUM_CHANNELS:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(",")
                    inverted_str = parts[1] if len(parts) > 1 else "0"
                    min_str = parts[2].strip() if len(parts) > 2 else ""
                    max_str = parts[3].strip() if len(parts) > 3 else ""

                    self.channels[i]["inverted"] = inverted_str.strip() == "1"
                    self.channels[i]["min_angle"] = float(min_str) if min_str != "" else None
                    self.channels[i]["max_angle"] = float(max_str) if max_str != "" else None
        except OSError:
            raise OSError(
                "Could not read {} -- run the calibration web app first.".format(self.config_file)
            )

    def _load_fret_positions(self):
        try:
            with open(self.fret_positions_file, "r") as f:
                self.fret_positions = json.load(f)
        except OSError:
            raise OSError(
                "Could not read {} -- calibrate fret positions first.".format(self.fret_positions_file)
            )

    def _raise_all(self):
        for string_num in range(1, NUM_STRINGS + 1):
            self._apply(lift_channel(string_num), self._raised_angle(string_num - 1))

    # -- calibration lookups --------------------------------------------
    def _raised_angle(self, string_idx):
        ch = self.channels[lift_channel(string_idx + 1)]
        low = ch["min_angle"] if ch["min_angle"] is not None else ANGLE_MIN
        high = ch["max_angle"] if ch["max_angle"] is not None else ANGLE_MAX
        return low if ch["inverted"] else high

    def _fret_entry(self, string_idx, fret):
        string_key = str(string_idx + 1)
        fret_key = str(fret)
        string_data = self.fret_positions.get(string_key, {})
        entry = string_data.get(fret_key)
        if entry is None:
            raise ValueError(
                "No calibrated position for string {} fret {} -- "
                "save it on the Fret Position Calibration tab first.".format(string_idx + 1, fret)
            )
        return entry

    def _finger_angle(self, string_idx, fret):
        return self._fret_entry(string_idx, fret)["finger"]

    def _lift_angle(self, string_idx, fret):
        return self._fret_entry(string_idx, fret)["lift"]

    # -- hardware --------------------------------------------------------
    def _apply(self, channel, angle):
        ch = self.channels[channel]
        low = ch["min_angle"] if ch["min_angle"] is not None else ANGLE_MIN
        high = ch["max_angle"] if ch["max_angle"] is not None else ANGLE_MAX
        angle = max(low, min(high, angle))
        ch["angle"] = angle
        self.pca.set_duty(channel, angle_to_duty(angle))

    # -- movement ----------------------------------------------------------
    def move(self, target_state, next_state=None,
             lift_delay_ms=DEFAULT_LIFT_DELAY_MS, press_delay_ms=DEFAULT_PRESS_DELAY_MS):
        """target_state/next_state: 4 values, index = string 1-4, value =
        fret to press (0 = open/lifted)."""
        if len(target_state) != NUM_STRINGS:
            raise ValueError("target_state must have exactly {} entries".format(NUM_STRINGS))
        if next_state is not None and len(next_state) != NUM_STRINGS:
            raise ValueError("next_state must have exactly {} entries".format(NUM_STRINGS))

        finger_targets = []
        for i in range(NUM_STRINGS):
            fret = target_state[i]
            if fret == 0:
                lookahead_fret = next_state[i] if (next_state is not None and next_state[i] != 0) \
                    else OPEN_STRING_DEFAULT_FRET
                finger_targets.append(self._finger_angle(i, lookahead_fret))
            else:
                finger_targets.append(self._finger_angle(i, fret))

        # 1) lift every finger clear of the fretboard before anything moves
        for i in range(NUM_STRINGS):
            self._apply(lift_channel(i + 1), self._raised_angle(i))

        time.sleep_ms(lift_delay_ms)

        # 2) slide fingers to their target frets while lifted
        for i in range(NUM_STRINGS):
            self._apply(finger_channel(i + 1), finger_targets[i])

        time.sleep_ms(press_delay_ms)

        # 3) press down the strings that are actually being fretted
        for i in range(NUM_STRINGS):
            fret = target_state[i]
            if fret != 0:
                self._apply(lift_channel(i + 1), self._lift_angle(i, fret))
