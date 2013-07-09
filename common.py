#!/usr/bin/env python

import logging
import sys

import gi
gi.require_version('Gst', '1.0')

from gi.repository import Gst

if not Gst.is_initialized():
    Gst.init(sys.argv)


## FIXME: tamano real mas luego.
## VIDEO_CAPS = Gst.Caps.from_string ('image/jpeg,width=800,heigth=448,rate=30,framerate=30/1')

## 16:9 , alcanza para tres camaras en un usb 2.0.
VIDEO_WIDTH = 1024
VIDEO_HEIGTH = 576
VIDEO_CAPS = Gst.Caps.from_string ('image/jpeg,width=%d,heigth=%d,rate=30,framerate=30/1' % (VIDEO_WIDTH, VIDEO_HEIGTH))
AUDIO_CAPS = Gst.Caps.from_string ('audio/x-raw,format=S16LE,rate=32000,channels=2')


INPUT_COUNT = 0
# seconds
WINDOW_LENGTH = 1.5
UPDATE_INTERVAL = .25
MIN_ON_AIR_TIME = 3
# dB
DEFAULT_NOISE_BASELINE = -45
NOISE_THRESHOLD = 6
SPEAK_UP_THRESHOLD = 3

MANUAL=False

XV_SYNC=False