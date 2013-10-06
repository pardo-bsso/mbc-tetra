import logging

import threading
import time
import sys
import os
from collections import deque
from itertools import ifilter


import gi
gi.require_version('Gst', '1.0')

from gi.repository import GObject
from gi.repository import Gst
from gi.repository import GstVideo
from gi.repository import GstController
from gi.repository import GLib

GObject.threads_init()
Gst.init(sys.argv)

from common import *
from output_sinks import AutoOutput, MP4Output, FLVOutput
from transitions import VideoMixerTransition, InputSelectorTransition



class TetraApp(GObject.GObject):
    __gsignals__ = {
       "level": (GObject.SIGNAL_RUN_FIRST, None, (GObject.TYPE_PYOBJECT, GObject.TYPE_PYOBJECT)),
       "insert-level": (GObject.SIGNAL_RUN_FIRST, None, (GObject.TYPE_PYOBJECT, GObject.TYPE_PYOBJECT)),
       "master-level": (GObject.SIGNAL_RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
       "prepare-xwindow-id": (GObject.SIGNAL_RUN_FIRST, None, (GObject.TYPE_OBJECT, GObject.TYPE_PYOBJECT)),
       "prepare-window-handle": (GObject.SIGNAL_RUN_FIRST, None, (GObject.TYPE_OBJECT, GObject.TYPE_PYOBJECT)),
       "source-disconnected": (GObject.SIGNAL_RUN_FIRST, None, [GObject.TYPE_OBJECT]),
       "record-started": (GObject.SIGNAL_RUN_FIRST, None, []),
       "record-stopped": (GObject.SIGNAL_RUN_FIRST, None, []),
       "state-changed": (GObject.SIGNAL_RUN_FIRST, None, (GObject.TYPE_PYOBJECT, GObject.TYPE_PYOBJECT, GObject.TYPE_PYOBJECT)),
    }
    def __init__(self):
        GObject.GObject.__init__(self)
        self.current_input = None
        self._automatic = True
        self._initialized = False
        self._rec_ok_cnt = 0
        self._about_to_record = False
        self._recording = False
        self._to_remove = {}
        self._remove_lck = threading.Lock()

        self.noise_baseline = DEFAULT_NOISE_BASELINE
        self.speak_up_threshold = SPEAK_UP_THRESHOLD
        self.min_on_air_time = MIN_ON_AIR_TIME

        self.last_switch_time = time.time()

        self.pipeline = pipeline = Gst.Pipeline.new ('pipeline')

        self.mixer = VideoMixerTransition()
        self.inputsel = self.mixer.mixer
        self.vconvert = Gst.ElementFactory.make ('videoconvert', None)

        self.pipeline.add (self.inputsel)
        self.pipeline.add (self.vconvert)
        #self.vsink = Gst.ElementFactory.make ('fakesink', None)
        q = Gst.ElementFactory.make ('queue2', None)
        self.vsink = Gst.ElementFactory.make ('tee', 'tetra main video T')
        self.pipeline.add (q)
        self.pipeline.add (self.vsink)
        self.inputsel.link_filtered(q, VIDEO_CAPS_SIZE)
        q.link(self.vconvert)
        self.vconvert.link(self.vsink)
        self.preview_sinks = []

        self.asink = Gst.ElementFactory.make ('tee', 'tetra main audio T')

        self.audio_avg = {}
        self.audio_peak = {}

        self.backgrounds = []
        self.inputs = []
        self.outputs = []
        self.audio_inserts = []
        self.video_inputs = []
        self.volumes = []
        self.levels = []
        self.amixer = Gst.ElementFactory.make ('adder', None)
        self.amixer.set_property('caps', AUDIO_CAPS)
        self.insert_mixer = Gst.ElementFactory.make ('adder', None)
        self.insert_mixer.set_property('caps', AUDIO_CAPS)

        self.cam_vol = Gst.ElementFactory.make ('volume', None)
        self.pipeline.add(self.cam_vol)
        self.master_vol = Gst.ElementFactory.make ('volume', None)
        self.master_level = Gst.ElementFactory.make ('level', None)
        self.master_level.set_property ("message", True)
        self.pipeline.add(self.master_vol)
        self.pipeline.add(self.master_level)

        qam = Gst.ElementFactory.make ('queue2', None)
        self.pipeline.add(qam)
        self.pipeline.add(self.amixer)
        self.pipeline.add(self.insert_mixer)
        self.pipeline.add(self.asink)

        self.amixer.link(qam)
        qam.link(self.cam_vol)
        self.cam_vol.link(self.insert_mixer)

        q = Gst.ElementFactory.make('queue2', None)
        self.pipeline.add(q)
        self.insert_mixer.link(self.master_vol)
        self.master_vol.link(self.master_level)
        self.master_level.link(q)
        q.link(self.asink)

        sink = AutoOutput()
        self.live_sink = sink.preview_sink
        self.add_output_sink(sink)

        sink = FLVOutput()
        self.add_output_sink(sink)



    def add_output_sink(self, sink):
        self.pipeline.add(sink)
        self.outputs.append(sink)
        self.vsink.link_pads('src_%u', sink, 'videosink')
        self.asink.link_pads('src_%u', sink, 'audiosink')

        sink.initialize()
        sink.connect('ready-to-record', self._start_record_ok)
        sink.connect('record-stopped', self._record_stopped)
        sink.sync_state_with_parent()

    def add_input_source(self, source):
        source.connect('ready-to-record', self._start_record_ok)
        source.connect('record-stopped', self._record_stopped)
        self.audio_avg[source] = deque (maxlen=WINDOW_LENGTH * 10)
        self.audio_peak[source] = deque (maxlen=WINDOW_LENGTH * 10)

        self.pipeline.add(source)
        self.inputs.append(source)

        self.mixer.add_input_source(source)
        source.link_pads('audiosrc', self.amixer, 'sink_%u')

        if source.xvsink:
            self.preview_sinks.append(source.xvsink)
        self.volumes.append(source.volume)
        self.levels.append(source.level)

        source.initialize()
        self.current_source = source
        logging.debug('ADD INPUT SOURCE, SYNC WITH PARENT: %s', source.sync_state_with_parent())
        #self.set_active_input_by_source(source, transition=False)
        if self.pipeline.get_state(0)[1] != Gst.State.PLAYING:
            if self.inputs or self.backgrounds:
                self.pipeline.set_state(Gst.State.PLAYING)
            else:
                self.start()
        self.pipeline.recalculate_latency()
        logging.debug('ADD INPUT SOURCE , PIPE STATE: %s', self.pipeline.get_state(0))
        GLib.idle_add(self._set_xvsync)

    def add_background_source(self, source, xpos=0, ypos=0):

        self.pipeline.add(source)
        self.backgrounds.append(source)

        self.mixer.add_background_source(source)
        source.link_pads('audiosrc', self.amixer, 'sink_%u')

        self.preview_sinks.append(source.xvsink)

        source.initialize()
        source.sync_state_with_parent()
        self.pipeline.recalculate_latency()

        GLib.idle_add(self._set_xvsync)

    def add_audio_insert(self, source):
        self.pipeline.add(source)
        self.audio_inserts.append(source)

        source.link_filtered(self.insert_mixer, AUDIO_CAPS)

        #self.volumes.append(source.volume)
        #self.levels.append(source.level)

        source.initialize()
        source.sync_state_with_parent()
        Gst.debug_bin_to_dot_file(self.pipeline, Gst.DebugGraphDetails.NON_DEFAULT_PARAMS | Gst.DebugGraphDetails.MEDIA_TYPE , 'debug_add_insert')

    def mute_channel (self, chanidx, mute):
        try:
            self.volumes[chanidx].set_property('mute', mute)
        except IndexError:
            pass

    def set_channel_volume(self, chanidx, volume):
        if volume > 1.5:
            volume = 1.5
        elif volume < 0:
            volume = 0

        try:
            self.volumes[chanidx].set_property('volume', volume)
        except IndexError:
            pass

    def set_audio_source(self, source):
        if source not in ['internal', 'external']:
            return
        if source == 'internal':
            for src in self.audio_inserts:
                src.set_mute(True)
            self.cam_vol.set_property('mute', False)
        else:
            self.cam_vol.set_property('mute', True)
            for src in self.audio_inserts:
                src.set_mute(False)

    def set_automatic(self, auto=True):
        self._automatic = auto

    def set_active_input_by_source(self, source, *args, **kwargs):
        self.current_input = self.mixer.set_active_input_by_source(source, *args, **kwargs)

    def set_active_input(self, inputidx):
        isel = self.inputsel
        oldpad = isel.get_property ('active-pad')
        if oldpad is None:
            return
        pads = isel.sinkpads
        idx = inputidx % len(pads)

        newpad = pads[idx]
        self.current_input = idx
        if idx != pads.index(oldpad):
            logging.info('SET ACTIVE INPUT inputidx: %d idx: %d', inputidx, idx)
            isel.set_property('active-pad', newpad)
##             s = Gst.Structure ('GstForceKeyUnit')
##             s.set_value ('running-time', -1)
##             s.set_value ('count', 0)
##             s.set_value ('all-headers', True)
##             ev = Gst.event_new_custom (Gst.EVENT_CUSTOM_UPSTREAM, s)
##             self.video_inputs[idx].send_event (ev)

    def toggle (self, *args):
        e = self.inputsel
        s = e.get_property ('active-pad')
        # pads[0] output, rest input sinks.
        # set_active_input() uses 0..N, so this works out to switch to the next
        i = e.pads.index(s)
        self.set_active_input(i)

    def __initialize(self):
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::state-changed", self.bus_state_changed_cb)
        bus.connect("message::element", self.bus_element_cb)
        bus.connect("message", self.bus_message_cb)
        bus.enable_sync_message_emission()
        bus.connect("sync-message::element", self.bus_sync_message_cb)

        self.tid = GLib.timeout_add(int (UPDATE_INTERVAL * 1000), self.process_levels)

        self._initialized = True
        self._last_state = [None, None, None]

    def __init_inputs(self):
        for src in self.inputs:
            src.initialize()
        for src in self.audio_inserts:
            src.initialize()

    def __init_outputs(self):
        for sink in self.outputs:
            sink.initialize()

    def start(self):
        firsttime=False
        if not self._initialized:
            self.__initialize()
            firsttime=True

        state = self.pipeline.get_state(0)
        if state[1] in [Gst.State.READY, Gst.State.PAUSED]:
            firsttime=True

        # if started with no cameras connected we need to set the state
        # of every input manually. (we call start() again when new devices are
        # added to sync everything)
        logging.debug('STARTING, CURRENT STATE: %s', state)
        if state[1] in [Gst.State.READY, Gst.State.PAUSED]:
            for src in self.inputs:
                src.initialize()
                src.sync_state_with_parent()
            for src in self.audio_inserts:
                src.initialize()
                src.sync_state_with_parent()

            for sink in self.outputs:
                sink.initialize()
                sink.sync_state_with_parent()

    ##    for src in self.inputs:
    ##        src.sync_state_with_parent()
    ##    for src in self.audio_inserts:
    ##        src.sync_state_with_parent()
    ##    for sink in self.outputs:
    ##        sink.sync_state_with_parent()

        if firsttime:
            def f():
                ret = self.pipeline.set_state (Gst.State.PLAYING)
                GLib.idle_add(self._set_xvsync)
                logging.debug('STARTING ret= %s', ret)
            self.pipeline.set_state (Gst.State.READY)
            GLib.timeout_add(100, f)
            return

        ret = self.pipeline.set_state (Gst.State.PLAYING)
        GLib.idle_add(self._set_xvsync)

        logging.debug('STARTING ret= %s', ret)
        GLib.timeout_add(int (2 * WINDOW_LENGTH * 1000), self.calibrate_bg_noise)


    def _start_record_ok(self, sink, *data):
        if self._rec_ok_cnt:
            self._rec_ok_cnt -= 1
        if self._rec_ok_cnt == 0:
            self.pipeline.set_state(Gst.State.PLAYING)
            self._recording = True
            self.emit('record-started')

    def __start_file_recording(self):
        # this is a quick and dirty way to avoid negotiation errors.
        # We go to READY and when every output capable of file writing
        # has replaced the old stream writer with a fresh one we go again
        # into PLAYING.
        self._rec_ok_cnt = 0

        self.pipeline.set_state(Gst.State.READY)
        for out in self.outputs:
            if out.start_file_recording():
                self._rec_ok_cnt += 1
        for inp in self.inputs:
            if inp.start_file_recording():
                self._rec_ok_cnt += 1

        # no sink or source was able to start recording
        if self._rec_ok_cnt == 0:
            self.pipeline.set_state(Gst.State.PLAYING)

    def _record_stopped(self, sink, *data):
        if self._rec_stop_cnt:
            self._rec_stop_cnt -= 1
            if self._rec_stop_cnt == 0:
                if self._about_to_record:
                    self.__start_file_recording()
                    self._about_to_record = False
                else:
                    if self._recording and self.pipeline.get_state(0)[1] == Gst.State.PLAYING:
                        self.pipeline.set_state (Gst.State.READY)
                        self.pipeline.set_state (Gst.State.PLAYING)
                        self._recording = False
                    self.emit('record-stopped')

    def start_file_recording(self):
        if self.pipeline.get_state(0)[1] != Gst.State.PLAYING:
            return

        self._rec_stop_cnt = len(self.outputs) + len(self.inputs)
        self._about_to_record = True
        for out in self.outputs:
            out.stop_file_recording()

        for inp in self.inputs:
            inp.stop_file_recording()


    def stop_file_recording(self):
        if not self._recording:
            return
        self._rec_stop_cnt = len(self.outputs) + len(self.inputs)
        for out in self.outputs:
            out.stop_file_recording()

        for inp in self.inputs:
            inp.stop_file_recording()

    def calibrate_bg_noise (self, *args):
        bgnoise = 0
        lavg = len (self.audio_avg)
        if lavg != 0:
            for source, q in self.audio_avg.items():
                bgnoise += sum (q) / (10*WINDOW_LENGTH)
            bgnoise /= lavg
        else:
            bgnoise = DEFAULT_NOISE_BASELINE
        self.noise_baseline = bgnoise
        logging.info('NOISE BG: %s', bgnoise)

# XXX: devolver True, sino el timeout se destruye
    def process_levels (self):
        # Until I get to code a better and more mathy algorithm this is how it works:
        # If all the sources are within a band from the background noise we switch to the next.
        # If all of them are above and within a band from the combined average level we also switch to the next.
        # If one increases the average level above certain threshold and it is also above the background
        # noise we switch to that.
        # Else, we switch to the one that has the maximum level in the current window.
        # Above all, no decision is taken if we are within less than the minimum on air time from the
        # last switching time or manual control is desired.
        if not self._automatic:
            return True

        now = time.time()
        def do_switch (src):
            if src == self.current_input:
                return
            self.last_switch_time = now
            self.set_active_input_by_source (src)
            logging.debug('DO_SWITCH %s', src)
        def do_rotate():
            self.last_switch_time = now
            if not self.inputs:
                return
            try:
                idx = self.inputs.index(self.current_input)
            except ValueError:
                idx = 0
            src = self.inputs[(idx+1) % len(self.inputs)]
            self.set_active_input_by_source (src)
            logging.debug('DO_ROTATE')

        if (now - self.last_switch_time) < self.min_on_air_time:
            return True

        dpeaks = []
        avgs = []
        above = []
        silent = True
        for source,q in self.audio_avg.items():
            if len(q) == 0:
                logging.debug('empty level queue source= %s', source)
                return True
            avg = sum (q) / (10*WINDOW_LENGTH)
            dp = (q[-1] - q[0])
            avgs.append ( (source, avg) )
            dpeaks.append ( (source, dp) )
            if abs (avg-self.noise_baseline) > NOISE_THRESHOLD:
                silent = False
                above.append( (source, avg) )
        if silent:
            logging.info('ALL INPUTS SILENT, ROTATING')
            do_rotate ()
            return True

        if len(above) == len(avgs):
            tavg = sum(x[1] for x in avgs)
            tavg /= len(above)
            ok = True
            for source, avg in avgs:
                if abs(avg-tavg) > self.speak_up_threshold:
                    ok = False
            if ok:
                logging.info('EVERYBODY IS TALKING(?), ROTATING')
                do_rotate()
                return True

# ver caso si mas de uno pasa umbral.
# un muting a channel gives a 600 something peak (from minus infinity to the current level)
        peaks_over = filter (lambda x: (x[1] > self.speak_up_threshold) and (x[1] < 60), dpeaks)
        if peaks_over:
            source, peak = max (peaks_over, key= lambda x: x[1])
            logging.debug('PEAKS OVER %s', peaks_over)
            # the result of filter() is [(source, avg)]
            avg = filter(lambda x: (x[0] is source), avgs)[0][1]
            if abs(avg - self.noise_baseline) > NOISE_THRESHOLD:
                logging.info('NEW VOICE, SWITCHING TO %s', source)
                do_switch (source)
                return True

        logging.info('SWITCHING TO THE LOUDEST %s', source)
        source, avg = max (avgs, key= lambda x: x[1])
        do_switch (source)

###        print ' AVGs ', avgs , ' dPEAKs ', dpeaks
        return True

    def _set_xvsync(self, *args):
        try:
            self.live_sink.set_property('sync', XV_SYNC)
            self.live_sink.expose()
        except:
            pass

        for sink in self.preview_sinks:
            try:
                sink.set_property('sync', XV_SYNC)
                sink.expose()
            except:
                continue

    def source_removed_cb (self, source):
        logging.debug('SOURCE REMOVED CB %s', source)
        if source in self.pipeline.children:
            self.pipeline.remove(source)
        logging.debug('SOURCE BIN REMOVED FROM PIPELINE OK')
        for coll in [self._to_remove, self.audio_avg, self.audio_peak]:
            try:
                coll.pop(source)
            except KeyError:
                pass
        logging.debug('SOURCE BIN REMOVED POP FROM COLL OK')

        for idx, sink in enumerate(self.preview_sinks):
            if sink in source:
                self.preview_sinks.pop(idx)
                break
        logging.debug('SOURCE BIN REMOVED SINK POP OK')


        if not self.inputs:
            if not self.backgrounds:
                def go_to_null():
                    self.pipeline.set_state(Gst.State.NULL)
                    logging.debug('SOURCE BIN REMOVED OK')
                    return False
                self.emit('source-disconnected', source)
                GLib.timeout_add(10, go_to_null)
                return
#        else:
###            self.__init_inputs()
###            self.__init_outputs()
###
###            self.pipeline.set_state(Gst.State.PLAYING)
##            self.pipeline.recalculate_latency()
##            GLib.idle_add(self._set_xvsync)

        self.pipeline.set_state(Gst.State.PLAYING)
        self.pipeline.recalculate_latency()

        self.emit('source-disconnected', source)

        logging.debug('SOURCE BIN REMOVED OK')
        Gst.debug_bin_to_dot_file(self.pipeline, Gst.DebugGraphDetails.NON_DEFAULT_PARAMS | Gst.DebugGraphDetails.MEDIA_TYPE , 'debug_core_source_removed')

    def bus_sync_message_cb (self, bus, msg):
        if msg.get_structure() is None:
            return True
        s = msg.get_structure()
        if s.get_name() in  ("prepare-xwindow-id", "prepare-window-handle"):
            self.emit (s.get_name(), msg.src, msg.src.get_parent())
            return True

    def bus_element_cb (self, bus, msg, arg=None):
        if msg.get_structure() is None:
            return True

        s = msg.get_structure()
        if s.get_name() == "ready-to-unlink":
            msg.src.do_unlink()

        if s.get_name() == "unlinked":
            self.source_removed_cb(msg.src)

        if s.get_name() == "level":
            parent = msg.src.get_parent()
            arms = s.get_value('rms')
            apeak = s.get_value('peak')
            larms = len(arms)
            lapeak = len(arms)
            if larms and lapeak:
                rms = sum (arms) / len (arms)
                peak = sum (apeak) / len (apeak)
                if parent in self.inputs:
                    self.audio_avg[parent].append (rms)
                    self.audio_peak[parent].append (peak)
                    #logging.debug('LEVEL idx %d, avg %f peak %f', idx, rms, peak)
                    self.emit('level', parent, apeak)
                elif parent in self.audio_inserts:
                    self.emit('insert-level', parent, apeak)
                elif msg.src is self.master_level:
                    self.emit('master-level', apeak)
        return True

    def bus_message_cb (self, bus, msg, arg=None):
        def log_error():
            logging.error('Gst msg ERORR src: %s msg: %s', msg.src, msg.parse_error())
            logging.debug('Gst msg ERROR CURRENT STATE %s', self.pipeline.get_state(0))
            Gst.debug_bin_to_dot_file(self.pipeline, Gst.DebugGraphDetails.NON_DEFAULT_PARAMS | Gst.DebugGraphDetails.MEDIA_TYPE , 'debug_core_error')

        if msg.type == Gst.MessageType.CLOCK_LOST:
            self.pipeline.set_state (Gst.State.PAUSED)
            self.pipeline.set_state (Gst.State.PLAYING)
        elif msg.type == Gst.MessageType.ERROR:
            parent = msg.src.get_parent()
            if parent in self.inputs:
                self._remove_lck.acquire()

                idx = self.inputs.index(parent)
                self.inputs.pop(idx)
                self._to_remove[parent] = idx
                if self.inputs:
                    # input-selector doesn't quite like when you remove/unlink the active pad.
                    self.set_active_input_by_source(self.inputs[0], transition=False)
                else:
                    if self.backgrounds:
                        source = self.backgrounds[0]
                        self.set_active_input_by_source(source, transition=False)
                parent.disconnect_source()
                log_error()
                self._remove_lck.release()

            if parent not in self._to_remove:
                log_error()

        return True

    def bus_state_changed_cb (self, bus, msg, arg=None):
        if msg.src != self.pipeline:
            return True
        prev, new, pending = msg.parse_state_changed()
        curr_state = [prev, new, pending]
        if new != self._last_state[1]:
            self.emit('state-changed', prev, new, pending)
        logging.debug('STATE CHANGE: %s', curr_state)
        self._last_state = curr_state

        return True

