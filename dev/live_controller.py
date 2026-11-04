"""Active runtime controller for the Native Qt window.

This file has no dialog or widget builders. The Native view owns presentation;
this controller coordinates audio, MIDI and SoundFont runtime ports.
"""

from __future__ import annotations

import os
import queue
from contextlib import contextmanager, nullcontext
from dataclasses import replace

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog

import audio_engine as ae
import audio_output
from audio_output import AudioSession, AudioStartError, AudioDeviceCatalog
from effect_control import EffectEvents
from effect_control import EffectRuntime
from midi_input_runtime import (MidiInputRuntime, native_source_selection_adapters,
    native_source_transactions, native_source_unlock_owners)
from bass_bindings import (
    BASS_MIDI_ASYNC, BASS_MIDI_NODRUMPARAM, BASS_SAMPLE_FLOAT, BASS_STREAM_DECODE, bass, bassmidi,
)
from settings import LIVE_PREFERENCE_FIELDS, load_engine_preferences
from event_log import log_text, LogHistory
from i18n import tr
from product import ENV_UI_ONLY
from windows_integration import set_current_process_priority, set_midi_mapper_enabled
from soundfont_files import SoundFontGroup, display_name, load_sfg, soundfont_load_filter
from soundfont_runtime import SoundFontRuntime, SoundFontRuntimeError
def midi_stream_creation_flags(preferences, decode_output=False):
    """Apply the saved live-MIDI timing choice when creating the stream.

    BMSynth 0046B0DA..0046B10E / 0046AF4A..0046AF75 omit
    BASS_MIDI_ASYNC, which remains ABMSynth's default. Opting in enables
    BASSMIDI's next-cycle intra-block timing for live events. Float headroom
    and decode output remain independent stream-creation choices. Float
    headroom is fixed for every backend, including BASSWASAPI's required
    floating-point decoding channel.
    """
    # Source 00476C58..00476CD8 sets all drum-key reverb/chorus sends to
    # full at reset. NODRUMPARAM makes that the new DLL's default as well;
    # it also retains the full UserFX input needed by the drum delay tap.
    flags = BASS_SAMPLE_FLOAT | BASS_MIDI_NODRUMPARAM
    if preferences.midi_async:
        flags |= BASS_MIDI_ASYNC
    if decode_output:
        flags |= BASS_STREAM_DECODE
    return flags


class LiveController:
    """Runtime boundary for the active native presentation.

    The controller does not construct or own a top-level QWidget.  The native
    window supplies the presentation methods and Qt lifetime; this object
    owns engine preferences, event routing, timers and SoundFont operations.
    """

    def initialize_controller(self, *, audio_session=None, effect_events=None, device_catalog=None,native_session_builder=None):
        self.effect_events = effect_events or EffectEvents(
            bassmidi, lambda: ae.runtime.stream_handle,
            lambda operation: self.append_log(log_text("runtime.effect_event_failed", operation=operation, error=bass.BASS_ErrorGetCode())),
        )
        self.effect_runtime = EffectRuntime(ae, self.effect_events)
        self.midi_input_runtime = MidiInputRuntime(ae, bassmidi, lambda: ae.runtime.stream_handle)
        self.native_session_builder=native_session_builder
        from settings import SettingsReadReport
        self._settings_read_report=self.__dict__.get('_settings_read_report') or SettingsReadReport()
        self._engine_preferences = load_engine_preferences(report=self._settings_read_report)
        self._pending_engine_preferences = self._engine_preferences
        self._asio_dll = None
        self._wasapi_dll = None
        self.audio_session = audio_session or AudioSession(bass, bassmidi, ae, audio_output)
        self.audio_session.telemetry = getattr(self, "audio_telemetry", None)
        if self.audio_session.telemetry is not None:
            self.audio_session.telemetry.session = self.audio_session
        self.device_catalog = device_catalog or AudioDeviceCatalog(bass, audio_output)
        self.soundfont_runtime = SoundFontRuntime(
            bass=bass,
            bassmidi=bassmidi,
            stream_handle=lambda: ae.runtime.stream_handle,
            engine_preferences=lambda: self._engine_preferences,
            configure_preset_controls=ae.configure_soundfont_group_presets,
            configure_performance=ae.configure_soundfont_group_performance,
            update_preset_control=ae.update_soundfont_group_preset,
            configure_available_presets=ae.configure_soundfont_group_available_presets,
            configure_chord_sources=ae.configure_chord_sources,
            preloaded_used=getattr(self, "_font_preloaded_used", None),
        )
        self.soundfont_runtime.commit_scope=self._soundfont_commit_scope
        self.soundfont_runtime.scalar_scope=self._soundfont_scalar_scope
        self.soundfont_runtime.control_lock=self._font_preload_lock
        self.soundfont_runtime.preset_tone_submitter=ae.runtime.submit_preset_tone
        self.soundfont_runtime.control_recovery=self._recover_soundfont_source_owner
        self.soundfont_runtime.control_commit_scope=self._soundfont_control_scope
        self.soundfont_runtime.before_mapping_change=getattr(self.audio_session,'stop_tuning_audition',lambda:None)
        self.build_ui(); self.set_dark(True)
        if not os.environ.get(ENV_UI_ONLY):
            try:
                set_current_process_priority(self._engine_preferences.priority)
            except OSError as exc:
                self.append_log(log_text("runtime.priority_failed", error=exc))
            try:
                set_midi_mapper_enabled(self._engine_preferences.windows_midi_default)
            except OSError as exc:
                self.append_log(log_text("player.mapper_preference_failed",error=exc))
        self.status_timer = QTimer(self); self.status_timer.timeout.connect(self.update_status); self.status_timer.start(100)
        self.log_timer = QTimer(self); self.log_timer.timeout.connect(self.process_log); self.log_timer.start(30)
        # Initial device enumeration is scheduled by the native window after
        # its first paint. A zero timer here can run before the parent window
        # is actually drawn, making the SoundFont progress dialog appear first.

    # Presentation is supplied by NativeLiveWindow.build_ui().

    @property
    def _soundfont_handles(self):
        return self.soundfont_runtime.handles

    @_soundfont_handles.setter
    def _soundfont_handles(self, value):
        self.soundfont_runtime.handles = list(value)

    @property
    def _soundfont_group(self):
        return self.soundfont_runtime.group

    @_soundfont_group.setter
    def _soundfont_group(self, value):
        self.soundfont_runtime.group = value

    def append_log(self, text): ae.append_log(text)
    def bass_event(self, event_id, value):
        return self.effect_runtime.send_event(event_id, value)

    def on_delay(self): self.effect_runtime.set_delay(self._read_delay_state())
    def begin_tuning_preview(self, request):
        from audio_output import InstrumentAuditionSource,AUDITION_NOTE_VELOCITY
        with self._soundfont_scalar_scope() as acquired:
            if not acquired or not self.audio_session.initialized:return None
            prepared=request.get('prepared_source')
            if prepared is not None and prepared.closed:return None
            parent=prepared.stream if prepared is not None else ae.runtime.stream_handle
            self.audio_session.stop_tuning_audition()
            source=InstrumentAuditionSource.for_tuning(parent,request)
            try:
                self.audio_session.install_instrument_audition(source,tuning=True)
                if not request.get('phrase') and not request.get('one_shot'):source.note_on(request['note'],AUDITION_NOTE_VELOCITY)
            except BaseException:
                source.close();raise
            return source

    def update_tuning_preview(self,source,request):
        # Do not hold a private reader lock while waiting for a font commit's
        # parent lock. Busy source work yields this UI-only refresh.
        prepared=request.get('prepared_source')
        if prepared is not None and prepared.closed:source.close();return
        with self._soundfont_scalar_scope() as acquired:
            if acquired:source.update_tuning(request)

    def reference_tuning_request(self):
        """Global pitch reference:stable electronic source,not channel1."""
        if not self.audio_session.initialized:return None
        return dict(note=69,identity='global-reference',source=self.soundfont_runtime.reference_tone_source(),
                    drum=False,profile={},preset={},raw_events=((23,0),(24,0),(43,0)))

    def audition_request(self,*,channel=None,effect=None):
        """UI request data;cloned MIDI/FX owners provide the audible state."""
        from midi_player import instrument_audition_phrase
        stream=ae.runtime.stream_handle
        if not stream or not self.audio_session.initialized:return None
        from bass_bindings import bassmidi
        request=dict(note=69,channel=channel if channel is not None else 0,
                     identity=('channel',channel) if channel is not None else ('effect',effect))
        if channel is not None:
            drum=bool(bassmidi.BASS_MIDI_StreamGetEvent(stream,channel,6))
            program=bassmidi.BASS_MIDI_StreamGetEvent(stream,channel,2)
            bank=bassmidi.BASS_MIDI_StreamGetEvent(stream,channel,10)
            lsb=bassmidi.BASS_MIDI_StreamGetEvent(stream,channel,70)
            request['identity']=('channel',channel,drum,bank,lsb,program)
            request.update(drum=drum,full_channel=True,
                channel_fx=ae.runtime.stream_effects.channel_fx.snapshot(channel))
            request['muted']=channel in ae.runtime.midi_options.mixer_muted
            from midi_input_runtime import MODULATION_NATIVE_FIELDS
            request['raw_events']=tuple((event,ae.runtime.performance.mod_observed[channel,field])
                for event,field in MODULATION_NATIVE_FIELDS.items() if (channel,field) in ae.runtime.performance.mod_observed)
            request['controllers']=tuple((cc,ae.runtime.auxiliary_cc.source_value(channel,cc)) for cc in range(21,25)
                if ae.runtime.channel_policy.destination(channel,f'aux_{cc}')==f'aux_{cc}'
                and not ae.runtime.midi_learn.targets_for_source(channel,cc))
            delay=ae.runtime.delay_runtime.states[channel]
            request['delay']=(delay.delay_type,delay.level,delay.time_ms,round(delay.feedback*100),delay.send)
        else:
            from midi_player import EFFECT_AUDITION_SCENARIOS,effect_audition_arrangement
            scenario=EFFECT_AUDITION_SCENARIOS[effect]
            tracks,phrase,period=effect_audition_arrangement(scenario)
            request.update(program=tracks[0][1],bank=0,bank_lsb=0,drum=False,
                           tracks=tracks,scenario=scenario,period=period)
            request['phrase']=tuple((at,length,key,max(1,ae.runtime.midi_options.velocity(velocity)),target)
                for at,length,key,velocity,target in phrase)
            delay=ae.runtime.delay_runtime.states[0]
            request['delay']=(delay.delay_type,delay.level,delay.time_ms,round(delay.feedback*100),
                80 if effect=='delay' else 0)
            sends=ae.runtime.channel_sends
            request['raw_events']=((23,sends.calculate(sends.global_reverb,sends.default_reverb)),
                (24,sends.calculate(sends.global_chorus,80 if effect=='chorus' else 0)),
                (43,80 if effect=='delay' else 0))
            return request
        phrase,family=instrument_audition_phrase(bank,program,drum=drum)
        request['phrase']=tuple((at,length,key,max(1,ae.runtime.midi_options.velocity(velocity)))
            for at,length,key,velocity in phrase)
        request['period']=32*60/116
        return request

    def apply_delay_preset(self, name):
        """Apply one row from BMSynth's runtime-built ten-entry delay list."""
        if getattr(self, "_preset_programmatic", False):
            return
        values = ae.BMSYNTH_DELAY_PRESETS.get(name)
        if values is None:
            return

        delay_type, level, time_ms, feedback = values
        was_programmatic = getattr(self, "_preset_programmatic", False)
        was_blocked = self.delay_type.blockSignals(True)
        self._preset_programmatic = True
        try:
            self.delay_type.setCurrentIndex(delay_type)
            self.delay_level.set(level, False)
            self.delay_time.set(time_ms, False)
            self.delay_feedback.set(feedback, False)
        finally:
            self.delay_type.blockSignals(was_blocked)
            self._preset_programmatic = was_programmatic
        self.on_delay()
    def on_velocity(self): self.effect_runtime.set_velocity(self.velocity.get())

    def apply_live_preferences(self, values):
        """Update live state only; pending creation settings remain separate."""
        previous = self._engine_preferences
        updated = replace(previous, **{name: getattr(values, name) for name in LIVE_PREFERENCE_FIELDS}).validate()
        changed = {name for name in LIVE_PREFERENCE_FIELDS if getattr(updated, name) != getattr(previous, name)}
        if not changed:
            return  # Inspect/unchanged Cancel must never fence the MIDI actor.
        # FontInit reads this same preference snapshot. Serialize the policy
        # transition with a concurrently staged group load so no newly
        # committed font can retain the previous send-combination mode.
        font_scope=(getattr(self, "_font_preload_lock", nullcontext())
                    if 'soundfont_fx_policy' in changed else nullcontext())
        with font_scope:
            if updated.soundfont_fx_policy != previous.soundfont_fx_policy:
                self.soundfont_runtime.apply_fx_policy(updated.soundfont_fx_policy)
            try:
                self.audio_session.apply_live_preferences(updated, previous)
                if updated.priority != previous.priority and not os.environ.get(ENV_UI_ONLY):
                    set_current_process_priority(updated.priority)
            except (AudioStartError, OSError, ValueError):
                self.audio_session.apply_live_preferences(previous, updated)
                if updated.soundfont_fx_policy != previous.soundfont_fx_policy:
                    self.soundfont_runtime.apply_fx_policy(previous.soundfont_fx_policy)
                raise
            if 'default_reverb_send' in changed:
                if ae.runtime.channel_command_executor is not None:
                    receipt=ae.runtime.submit_reset_policy({'default_reverb':updated.default_reverb_send},
                        on_complete=self._engine_effect_completed)
                    if not receipt.accepted:raise RuntimeError(tr('runtime.channel_executor_retained'))
                else:ae.set_default_reverb_send(updated.default_reverb_send)
            if changed.intersection(('cc6_direct_pitch_range', 'cc94_custom_delay_send')):
                ae.set_cc_interpretation(
                    cc6_direct_pitch_range=updated.cc6_direct_pitch_range,
                    cc94_custom_delay_send=updated.cc94_custom_delay_send,
                )
            if updated.auto_reset_delay_s != previous.auto_reset_delay_s:
                ae.configure_auto_reset(updated.auto_reset_delay_s)
            self._engine_preferences = updated
        if changed.intersection(('main_log_detail','main_log_level')):
            self._sync_main_log_display()
        if changed.intersection(('minimize_to_tray', 'close_to_tray')):
            self._sync_tray_preferences()

    @contextmanager
    def _soundfont_scalar_scope(self):
        """Nonblocking gain/document lease against font loader/free ownership."""
        lock=getattr(self,'_font_preload_lock',None)
        if lock is None:
            yield True;return
        acquired=lock.acquire(blocking=False)
        try:yield acquired
        finally:
            if acquired:lock.release()

    @contextmanager
    def _soundfont_commit_scope(self):
        """Stage assets while playing, fence only source publication.

        The loader thread binds the same EngineRuntime, never Qt. Waiting
        for the native FIFO lease must not hold the GUI's MIDI mutex.
        Startup/older images retain the original source transaction.
        """
        owner=self.midi_input_runtime.engine.runtime
        with owner.activate():
            factory=self.midi_input_runtime.native_factory
            if factory is None:
                yield
            else:
                # A finished loader may hand over only a publication-only
                # retained lease. Native unlock owners remain thread-bound.
                if not factory.retry_source_edit(allow_handover=True):
                    raise RuntimeError(tr('runtime.channel_executor_retained'))
                with factory.source_edit_scope(self.soundfont_runtime,preserve_activity=True):yield

    @contextmanager
    def _soundfont_control_scope(self):
        """Same compound setting publication as reset-policy edits,no assets.

        Font maps/sample borrowers are unchanged. Do not wait for unrelated
        future notes or retire/rebuild first-use sample preparation.
        """
        owner=self.midi_input_runtime.engine.runtime
        with owner.activate():
            factory=self.midi_input_runtime.native_factory
            if factory is None:yield
            else:
                with factory.source_edit_scope(None,reset_contracts_only=True,preserve_activity=True):yield

    def _recover_soundfont_source_owner(self):
        """Called on the failing source worker, never from an audio callback."""
        factory=self.midi_input_runtime.native_factory
        owner=self.midi_input_runtime.engine.runtime
        with owner.activate():
            if factory is None:
                # Older-image source edits still use thread-affine BASS
                # publication locks. Absence of the new Factory is not proof
                # that this loader may exit after a failed unlock.
                for transaction in native_source_transactions(owner):
                    if transaction.pending is not None:transaction.run(0,())
                for adapter in native_source_selection_adapters(owner):
                    adapter.settle_failed_tickets()
                locks=native_source_unlock_owners(owner)
                return not bool(locks),locks
            factory.recover_source_edit_owner(timeout=5.0)
            locks=factory.source_edit_unlock_owners()
            if locks:return False,locks
            return bool(factory.release_source_edit_owner()),()

    def selected_output_device(self):
        """Return the BASS device ID, never an index into filtered UI rows."""
        device = self.device_combo.currentData()
        return -1 if device is None else device if isinstance(device, tuple) else int(device)

    def init_bass(self, *, preserve_soundfonts=False):
        retained=bool(preserve_soundfonts and self.soundfont_runtime.handles)
        if retained:self.cleanup_resources(preserve_soundfonts=True)
        else:self.cleanup_resources()
        preferences = self._engine_preferences
        ae.set_cc_interpretation(
            cc6_direct_pitch_range=preferences.cc6_direct_pitch_range,
            cc94_custom_delay_send=preferences.cc94_custom_delay_send,
        )
        selected = self.selected_output_device()
        stream_flags = midi_stream_creation_flags(preferences, isinstance(selected, tuple))
        try:
            interpolation_error = self.audio_session.open(selected, preferences, int(self.winId()), stream_flags, start_shared=False)
        except AudioStartError as exc:
            status_id = {
                "bass_init": "runtime.status_bass_init_failed",
                "midi_stream": "runtime.status_midi_stream_failed",
                "channel_play": "runtime.status_channel_play_failed",
            }.get(exc.stage, "runtime.status_midi_stream_failed")
            self.set_runtime_status(log_text(status_id, error=exc.code))
            return False
        if interpolation_error is not None:
            self.set_runtime_status(log_text("runtime.interpolation_failed", error=interpolation_error))
        try:self.soundfont_runtime.prepare_reference_tone()
        except SoundFontRuntimeError as error:self.append_log(str(error))
        if retained:
            try:self.soundfont_runtime.rebind_loaded_stream()
            except (OSError,RuntimeError) as error:
                self._report_soundfont_load_error(error)
                return False
        else:self.load_current_soundfont()
        ae.set_default_reverb_send(preferences.default_reverb_send)
        ae.reset_channel_effect_sends()
        self.apply_effect_state()
        # Start with the same complete Reset policy as the Reset command,
        # before accepting MIDI input or starting the physical output.
        ae.ensure_midi_port(max(row['slot'] for row in self.midi_input_runtime.port_settings.ports))
        from midi_input_runtime import publish_midi_port_names
        if not os.environ.get('ABMSYNTH_UI_ONLY'):
            publish_midi_port_names(self.midi_input_runtime.port_settings)
        self.midi_input_runtime.reset()
        try:
            if self.native_session_builder is not None:
                # Composition is explicit and after original startup
                # Reset/font/effect application, before input/physical playback.
                factory=self.native_session_builder(self.midi_input_runtime,self.soundfont_runtime)
            else:factory=None
            if factory is not None:
                self.midi_input_runtime.bind_native_factory(factory)
                self.effect_events.bind_ordered_commands(ae.runtime.submit_engine_events,self._engine_effect_completed,
                    macro_submitter=ae.runtime.submit_engine_macro,batch_submitter=ae.runtime.submit_engine_batch)
                # Custom/pointer DSPs cannot use an out-of-order GUI bypass.
                if not ae.runtime.stream_effects.rack.bind_topology_commands(ae.runtime,factory.execution.library,
                        ae.runtime.submit_effect_topology):
                    raise RuntimeError(tr('runtime.channel_executor_retained'))
                if not ae.runtime.stream_effects.bind_ordered_commands(ae.runtime,factory.execution.library,
                        ae.runtime.submit_stream_effect,self._engine_effect_completed):
                    raise RuntimeError(tr('runtime.channel_executor_retained'))
                ae.runtime.stream_effects.channel_fx.bind(ae.runtime,factory.execution.library)
                if not ae.runtime.settle_channel_commands(timeout=5):
                    raise RuntimeError(tr('runtime.channel_executor_retained'))
                self.append_log(log_text('runtime.native_session_ready'))
        except Exception as error:
            # Qt timer exceptions must not leave a half-started engine that
            # looks ready. Retire confirmed owners, never force-free borrowers.
            self.set_runtime_status(log_text('runtime.native_session_start_failed',error=error))
            try:self.cleanup_resources()
            except Exception as cleanup_error:
                self.append_log(log_text('runtime.startup_cleanup_failed',error=cleanup_error))
            return False
        self.create_virtual_port()
        try:
            self.audio_session.start_output(self._asio_dll, self._wasapi_dll, preferences)
        except AudioStartError as exc:
            status_id = ("runtime.status_channel_play_failed" if exc.stage == "channel_play" else
                         "runtime.wasapi_start_failed" if exc.stage == "wasapi_start" else "runtime.asio_start_failed")
            self.set_runtime_status(log_text(status_id, error=exc.code))
            self.cleanup_resources()
            return False
        if self.audio_session.telemetry is not None:
            self.audio_session.telemetry.start_observation()
        return True
    def populate_devices(self):
        catalog = self.device_catalog.enumerate()
        self._asio_dll = catalog.asio_dll
        self._wasapi_dll = catalog.wasapi_dll
        if catalog.asio_error is not None:
            self.append_log(log_text("runtime.asio_unavailable", error=catalog.asio_error))
        if catalog.wasapi_error is not None:
            self.append_log(log_text("runtime.wasapi_unavailable", error=catalog.wasapi_error))
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for device in catalog.devices:
            self.add_output_device_row(device)
        if not catalog.devices:
            self.add_output_device_row(audio_output.OutputDevice(None, -1))
        if self.device_combo.count():
            preferred = getattr(self, "_preferred_output_device", None)
            endpoint_id = getattr(self, "_preferred_output_endpoint_id", None)
            match = next((index for index in range(self.device_combo.count())
                          if (endpoint_id and isinstance(self.device_combo.itemData(index), tuple)
                              and self.device_combo.itemData(index)[:1] == ("wasapi",)
                              and len(self.device_combo.itemData(index)) > 2
                              and self.device_combo.itemData(index)[2] == endpoint_id)), None)
            if match is None:
                match = next((index for index in range(self.device_combo.count())
                              if self.device_combo.itemText(index) == preferred), 0)
            self.device_combo.setCurrentIndex(match)
        self.device_combo.blockSignals(False)
        self._on_output_device_changed(self.device_combo.currentIndex())
    def load_current_soundfont(self):
        path=self.soundfont_path.text().strip(); return self.load_group(load_sfg(path) if path.lower().endswith(".sfg") and os.path.isfile(path) else SoundFontGroup.single_font(path) if path else SoundFontGroup())
    def load_group(self, group, *, persist=True):
        try:
            result = self.soundfont_runtime.load_group(group)
        except Exception as exc:
            return self._report_soundfont_load_error(exc)
        return self._present_loaded_soundfont_group(group, result)

    def _report_soundfont_load_error(self, error):
        # Presentation stays with the active Native window; this controller
        # forwards errors rather than constructing its own unstyled Qt box.
        self.show_soundfont_load_error(error)
        return False

    def _present_loaded_soundfont_group(self, group, result):
        """Update Qt only after the runtime has committed a valid font group."""
        if result is None:
            return False
        label = display_name(group) if group.fonts else tr("group.empty")
        self.soundfont_path.setText(group.source_path or (group.fonts[0].filename if group.fonts else ""))
        self.group_button.setText(label)
        self.append_log(log_text(
            "runtime.soundfont_loaded",
            label=label,
            font_count=result.font_count,
            mapping_count=result.explicit_mapping_count,
        ))
        return True

    def apply_group_runtime(self, group, change, *, persist=True):
        try:
            applied = self.soundfont_runtime.apply_runtime(group, change)
        except (SoundFontRuntimeError,TimeoutError) as exc:
            self.append_log(log_text('runtime.preset_controls_failed',error=str(exc)))
            return False
        return self.load_group(group, persist=persist) if applied is None else applied

    def queue_group_controls(self,group,change,on_complete):
        return self.soundfont_runtime.queue_runtime_controls(group,change,
            lambda receipt,snapshot:on_complete(receipt,None if snapshot is None else {'group':snapshot}))

    def enumerate_instruments(self, group=None):
        return self.soundfont_runtime.enumerate_instruments(group)

    def browse_font(self):
        path,_=QFileDialog.getOpenFileName(self,tr("group.select_soundfont_title"),"",soundfont_load_filter())
        if not path:
            return None
        previous = self.soundfont_path.text()
        self.soundfont_path.setText(path)
        if self.load_current_soundfont():
            return path
        self.soundfont_path.setText(previous)
        return None
    def apply_effect_state(self):
        """Apply one captured UI state through the same port used by edits."""
        applied = self.effect_runtime.apply_state(
            self.capture_effect_state(),
            lambda name: self.apply_reverb_preset(name, update_controls=False),
            lambda name: self.on_chorus_preset(name, update_controls=False),
        )
        if not applied:
            self.append_log(log_text("audio_fx.restore_failed"))
        with ae.control_source("configuration"):
            ae.runtime.performance.apply_all()
        # Default seeds are for a new stream or an explicit reset, not an
        # unrelated live effect edit that must leave current MIDI values alone.
        if ae.runtime.channel_policy_stream != ae.runtime.stream_handle:
            ae.apply_channel_reset_defaults()
            ae.runtime.channel_policy_stream = ae.runtime.stream_handle


    def reset(self):
        receipt=ae.runtime.submit_manual_reset(self.midi_input_runtime.reset)
        if receipt.successful:self.append_log(log_text("runtime.midi_reset"))
    def process_log(self):
        channel_fx=ae.runtime.stream_effects.channel_fx
        channel_fx.sync_midi()
        if getattr(self,'_channel_fx_midi_errors',0)!=channel_fx.midi_errors:
            self._channel_fx_midi_errors=channel_fx.midi_errors
            self.append_log(log_text('channel_editor.audio_fx_midi_failed'))
        if getattr(self,'_channel_fx_saved_revision',None)!=channel_fx.revision:
            self._channel_fx_saved_revision=channel_fx.revision
            self._queue_ui_save()
        if self.midi_input_runtime.port_router is not None:self.midi_input_runtime.port_router.poll()
        trace = self.midi_input_runtime.trace
        if trace is not None and trace.write_error:
            self.debug()  # Runtime owns capture; preferences presents its state.
        self.midi_input_runtime.poll_trace()
        lines=[]
        for _ in range(100):
            try: lines.append(ae.runtime.log_queue.get_nowait())
            except queue.Empty: break
        self._sync_main_log_display(lines)

    def _sync_main_log_display(self, entries=()):
        if not hasattr(self,'_main_log_history'):self._main_log_history=LogHistory()
        history=self._main_log_history
        added=history.extend(entries)
        preferences=getattr(self,'_engine_preferences',None)
        level=getattr(preferences,'main_log_level','info')
        detail=getattr(preferences,'main_log_detail','summary')
        policy=(level,detail)
        changed=policy!=getattr(self,'_main_log_policy',None)
        self._main_log_policy=policy
        if changed:
            self.log.setPlainText('\n'.join(history.visible(level,detail)))
            bar=self.log.verticalScrollBar();bar.setValue(bar.maximum())
        else:
            visible=[history.display(entry,detail) for entry in added if history.accepts(entry,level,detail)]
            if visible:self.log.appendPlainText('\n'.join(visible))
    def _engine_effect_completed(self,receipt):
        # Completion may run on the metadata thread. Queue-only logger and
        # captured worker error; no Qt and no thread-local error reread here.
        if not receipt.successful:
            if receipt.error or not receipt.unlock_error:
                ae.runtime.append_log(log_text('runtime.effect_event_failed',operation=receipt.operation,error=receipt.error))
            if receipt.unlock_error:
                ae.runtime.append_log(log_text('runtime.effect_event_failed',operation='BASS_ChannelLock',error=receipt.unlock_error))

    def cleanup_resources(self, *, preserve_soundfonts=False):
        # Preset commands need the live MIDI receipt owner until completion.
        # Device switches use this path as well as final application shutdown.
        if not self.soundfont_runtime.finish_runtime_controls():
            raise RuntimeError(tr('runtime.preset_controls_pending'))
        telemetry = getattr(self, "audio_telemetry", None)
        if telemetry is not None:
            telemetry.pause_observation()
        player = getattr(self, "_midi_player", None)
        if player is not None:
            if player.transport.stop_and_wait() is False:
                raise RuntimeError(tr('runtime.channel_executor_retained'))
        if self.midi_input_runtime.stop() is False:
            raise RuntimeError(tr('midi_pipe.native_shutdown_failed'))
        native_session=self.midi_input_runtime.native_factory is not None
        if self.midi_input_runtime.retire_native_session() is False:
            raise RuntimeError(tr('runtime.channel_executor_retained'))
        if native_session:
            self.effect_events.bind_ordered_commands()
            if not ae.runtime.stream_effects.rack.bind_topology_commands(ae.runtime,None):
                raise RuntimeError(tr('runtime.channel_executor_retained'))
            if not ae.runtime.stream_effects.bind_ordered_commands(ae.runtime,None):
                raise RuntimeError(tr('runtime.channel_executor_retained'))
        self.audio_session.quiesce(self._asio_dll)
        if preserve_soundfonts:
            # Original source/preload workers are already retired above. Fonts
            # are BASSMIDI-owned assets, not output-device/BASS stream handles.
            self.soundfont_runtime.retire_first_note_preparations()
        elif self.soundfont_runtime.close() is False:
            raise RuntimeError(tr("runtime.font_release_failed"))
        self.audio_session.close()

