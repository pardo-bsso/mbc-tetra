#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging

import sys
import time

import gi
gi.require_version('Gst', '1.0')

from gi.repository import GObject
from gi.repository import GLib
from gi.repository import Gst
from gi.repository import GstVideo
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import GdkX11

import cairo

GObject.threads_init()
Gst.init(sys.argv)
Gtk.init(sys.argv)
Gdk.init(sys.argv)

import config
from common import *

from tetra_core import TetraApp, INPUT_COUNT, DEFAULT_NOISE_BASELINE
from widgets import SoundMixWidget, PreviewWidget, MasterMonitor, PipManager, RecordWidget, NonliveWidget
from vlc import Vlc
import input_sources

class MainWindow(object):
    def __init__(self, app):
        settings = Gtk.Settings.get_default()
        settings.props.gtk_button_images = True
        self.app = app
        self.imon = input_sources.InputMonitor()
        self.pipgrab = True
        self.pipmgr = PipManager()
        self.pipmgr.connect('switch', self.switch_cam)
        self.pipmgr.connect('pip-start', self.pip_start)
        self.pipmgr.connect('pip-off', self.pip_off)

        app.connect('level', self.update_levels)
        app.connect('master-level', self.update_master_level)
        app.connect('source-disconnected', self.source_disconnected_cb)
        app.connect('prepare-window-handle', self.prepare_window_handle_cb)
        app.connect('state-changed', self.state_changed_cb)

        self.vlc = Vlc(silent=True)
        self.vlc.launch()

        self.imon.connect('added', self.source_added_cb)
        self.imon.start()

        self.builder = Gtk.Builder ()
        self.builder.add_from_file (config.get('main_ui', 'main_ui_tabbed.ui'))

        self.window = self.builder.get_object('tetra_main')
        self.window.connect ("destroy", lambda app: Gtk.main_quit())
        self.window.connect ("key-press-event", self.on_keypress)
        self.window.fullscreen ()

        self.preview_box = self.builder.get_object('PreviewBox')
        self.main_box = self.builder.get_object('MainBox')
        self.controls = self.builder.get_object('controls')
        self.options_box = self.builder.get_object('OptionsBox')

        self.sound_mix = SoundMixWidget()
        self.options_box.add(self.sound_mix)
        self.sound_mix.connect('set-mix-device', self.insert_sel_cb)
        self.sound_mix.connect('set-mix-source', self.insert_sel_cb)
        self.insert_sel_cb(self.sound_mix, None)

        pipbox = self.builder.get_object('PiPBox')
        if pipbox:
            pipbox.add(self.pipmgr)

        self.recwidget = None
        recbox = self.builder.get_object('RecBox')
        if recbox:
            self.recwidget = RecordWidget()
            recbox.add(self.recwidget)
            def rec_start(*args):
                app.start_file_recording()
            def rec_stop(*args):
                app.stop_file_recording()
            self.recwidget.connect('record-start', rec_start)
            self.recwidget.connect('record-stop', rec_stop)

        nonlivebox = self.builder.get_object('NonliveBox')
        if nonlivebox:
            self.nonlive = NonliveWidget()
            nonlivebox.add(self.nonlive)

        self.master_monitor = MasterMonitor()
        self.livebox = self.builder.get_object('LiveBox')
        self.livebox.pack_end(self.master_monitor, False, False, 0)

        self.sliders = []
        self.bars = []
        self.previews = {}

        self.window.show_all()

        for (src,props) in self.imon.get_devices():
            source = src(**props)
            self.add_source(source)
            self.app.add_input_source(source)

        live = self.builder.get_object('LiveOut')
        self.live = live
        self.live_xid = live.get_property('window').get_xid()
        live.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.TOUCH_MASK)
        live.connect('button-press-event', self.live_click_cb)
        #live.connect('draw', self.live_draw_cb)

        auto = self.builder.get_object('automatico')
        if auto:
            auto.connect('clicked', self.auto_click_cb)

        dbg = self.builder.get_object('dbg')
        if dbg:
            def state_cb(widget, state):
                logging.debug('STATE CB: %s', app.pipeline.set_state(state))
            self.builder.get_object('playing').connect('clicked', state_cb, Gst.State.PLAYING)
            self.builder.get_object('paused').connect('clicked', state_cb, Gst.State.PAUSED)
            self.builder.get_object('ready').connect('clicked', state_cb, Gst.State.READY)
            self.builder.get_object('null').connect('clicked', state_cb, Gst.State.NULL)

        app.live_sink.set_window_handle(live.get_property('window').get_xid())

        self.app.set_automatic(False)
        self.player = input_sources.InterPlayer()
        self.nonlive.set_player(self.player)
        self.nonlive.connect('pause', self.player_paused_cb)
        self.nonlive.connect('play', self.player_playing_cb)
        self.nonlive.connect('do-action', self.player_paused_cb)
        self.nonlive.connect('stop', self.player_paused_cb)

        inter = input_sources.InterSource()
        self.app.add_video_insert(inter)
        self.intersource = inter

    def player_playing_cb(self, player, *args):
        self.app.set_active_input_by_source(self.intersource, transition=False)
    def player_paused_cb(self, player, *args):
        if self.app.inputs:
            self.app.set_active_input_by_source(self.app.inputs[0], transition=False)

    def source_added_cb(self, imon, src, props):
        source = src(**props)
        preview = self.add_source()
        def _add_src():
            self.previews[source] = preview
            preview.set_source(source)
            self.app.add_input_source(source)
            Gst.debug_bin_to_dot_file(app.pipeline, Gst.DebugGraphDetails.NON_DEFAULT_PARAMS | Gst.DebugGraphDetails.MEDIA_TYPE | Gst.DebugGraphDetails.CAPS_DETAILS , 'source_added_cb')
        # XXX: FIXME: we should wait till pulseaudio releases the card.
        # (or disable it)
        GLib.timeout_add(9*1000, _add_src)

    def add_source(self, source=None):
        preview = PreviewWidget(source)
        self.preview_box.pack_start(preview, False, False, 0)
        preview.show()
        self.previews[source] = preview
        preview.connect('preview-clicked', self.preview_click_cb)
        return preview

    def insert_sel_cb (self, widget, arg):
        source = widget.mix_source
        devinfo = widget.mix_device
        device = devinfo['device']
        if source == 'external':
            if not self.app.audio_inserts:
                src = input_sources.AlsaInput({'device': device})
                self.app.add_audio_insert(src)
            else:
                self.app.audio_inserts[0].set_device(device)
        self.app.set_audio_source(source)

    def on_keypress (self, widget, event):
        if event.keyval == Gdk.KEY_F1:
            self.pipgrab = not self.pipgrab

        if self.pipgrab:
            self.pipmgr.on_keypress(widget, event)
            return True
        else:
            return False

    def pip_off(self, widget, idx):
        logging.debug('PiP off %d', idx)
        if idx == -1:
            for input in app.inputs:
                self.app.mixer.stop_pip(input)
        else:
            try:
                self.app.mixer.stop_pip(self.app.inputs[idx])
            except IndexError:
                pass

        if self.player.is_playing():
            self.app.set_active_input_by_source(self.intersource)

    def pip_start(self, widget, idx, pos):
        logging.debug('PiP start %d %s', idx, pos)
        try:
            self.app.mixer.start_pip(self.app.inputs[idx], pos)
        except IndexError:
            pass

    def switch_cam(self, widget, idx):
        logging.debug('Switch cam %d', idx)
        try:
            if idx == 9:
                self.app.set_active_input_by_source(self.intersource)
            else:
                self.app.set_active_input_by_source(self.app.inputs[idx])
        except IndexError:
            pass

    def auto_click_cb (self, widget):
        self.app.set_automatic(widget.get_active())

    def live_draw_cb (self, widget, cr):
        return False

    def live_click_cb (self, widget, event):
        self.controls.set_visible(not self.controls.get_visible())

    def preview_click_cb (self, widget, source):
        self.app.set_active_input_by_source (source)

    def prepare_window_handle_cb (self, app, xvimagesink, source):
        if source in self.previews:
            logging.debug('prepare window handle %s', source)
            self.previews[source].set_window_handle()
            return True
        elif xvimagesink is self.app.live_sink:
            Gdk.threads_enter ()
            xvimagesink.set_window_handle(self.live_xid)
            xvimagesink.set_property('sync', XV_SYNC)
            Gdk.threads_leave ()

    def source_disconnected_cb (self, app, source):
        logging.debug('SOURCE DISCONNECTED CB EN TETRA MAIN')
        if source in self.previews:
            logging.debug('SOURCE DISCONNECTED CB EN TETRA MAIN source en previews')
            preview = self.previews.pop(source)
            self.preview_box.remove(preview)
            preview.destroy()
            logging.debug('SOURCE DISCONNECTED CB EN TETRA MAIN source en previews REMOVIDA')
        return True

    def update_master_level (self, app, peaks):
        self.master_monitor.set_levels(peaks)

    def update_levels (self, app, source, peaks):
        if source in self.previews:
            self.previews[source].set_levels(peaks)

    def state_changed_cb(self, app, prev, current, new):
        if current != Gst.State.PLAYING:
            self.vlc.stop()
        else:
            self.vlc.start()


def load_theme(theme_name):
    dark = config.get('use_dark_theme', False)
    provider = None
    if dark:
        provider = Gtk.CssProvider.get_named(theme_name, 'dark') or Gtk.CssProvider.get_named(theme_name, None)
    else:
        provider = Gtk.CssProvider.get_named(theme_name, None)

    if provider is None:
        logging.error('Cannot load theme: %s', theme_name)
        return

    screen = Gdk.Screen.get_default()
    context = Gtk.StyleContext()
    context.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)

if __name__ == "__main__":

    logging.basicConfig(level=logging.DEBUG)


    theme = config.get('theme', None)
    if theme:
        load_theme(theme)

    app = TetraApp()

    w2 = MainWindow(app)

    app.start()

    Gst.debug_bin_to_dot_file(app.pipeline, Gst.DebugGraphDetails.NON_DEFAULT_PARAMS | Gst.DebugGraphDetails.MEDIA_TYPE | Gst.DebugGraphDetails.CAPS_DETAILS , 'debug_start')

    Gtk.main()
    sys.exit(0)

