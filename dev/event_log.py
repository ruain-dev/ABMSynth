"""Typed,localised application events;screen filtering never owns diagnostics."""
from collections import deque
from i18n import tr

LOG_LEVELS = ('info','warning','error')
MESSAGE_LEVELS = {'audio_fx.invalid': 'error',
 'audio_fx.restore_failed': 'error',
 'capability.modulation_observer_failed': 'warning',
 'channel_editor.audio_fx_midi_failed': 'error',
 'channel_editor.cc_save_failed': 'error',
 'channel_editor.function_failed': 'error',
 'midi_loss.detail': 'error',
 'midi_loss.notice_overflow': 'error',
 'midi_loss.sequence': 'error',
 'midi_loss.unavailable': 'error',
 'midi_mode.switched': 'info',
 'midi_pipe.callback_failed': 'error',
 'midi_pipe.creation_failed': 'error',
 'midi_pipe.invalid_frame': 'error',
 'midi_pipe.native_shutdown_failed': 'error',
 'midi_pipe.ready': 'info',
 'midi_pipe.session_rejected': 'error',
 'midi_pipe.startup_failed': 'error',
 'midi_ports.io_warning': 'error',
 'midi_ports.loop_blocked': 'warning',
 'midi_ports.missing': 'warning',
 'midi_ports.open_failed': 'error',
 'midi_recording.capture_failed': 'error',
 'native_meter.install_failed': 'error',
 'output_tools.record_loss': 'error',
 'output_tools.record_saved': 'info',
 'playback_info.sensor_failed': 'warning',
 'player.export_cleanup_failed': 'error',
 'player.mapper_preference_failed': 'error',
 'player.recovery_scan_failed': 'error',
 'player.stop_failed': 'error',
 'preferences.import_success': 'info',
 'runtime.asio_recovered': 'info',
 'runtime.asio_recovery_failed': 'error',
 'runtime.asio_start_failed': 'error',
 'runtime.asio_unavailable': 'warning',
 'runtime.auto_reset_idle': 'info',
 'runtime.chorus_preset_apply_failed': 'error',
 'runtime.debug_driver_unavailable': 'warning',
 'runtime.debug_flush_pending': 'warning',
 'runtime.debug_incomplete': 'error',
 'runtime.debug_start_failed': 'error',
 'runtime.debug_started': 'info',
 'runtime.debug_stop_failed': 'error',
 'runtime.debug_stopped': 'info',
 'runtime.debug_write_failed': 'error',
 'runtime.drum_delay_port_limited': 'warning',
 'runtime.effect_backend_failed': 'error',
 'runtime.effect_event_failed': 'error',
 'runtime.eq_apply_failed': 'error',
 'runtime.soundfont_fallback': 'warning',
 'runtime.extended_delay_apply_failed': 'error',
 'runtime.final_meter_create_failed': 'error',
 'runtime.final_meter_dsp_failed': 'error',
 'runtime.final_meter_sync_failed': 'error',
 'runtime.font_preload_failed': 'warning',
 'runtime.ini_warning': 'warning',
 'runtime.interpolation_failed': 'error',
 'runtime.midi_panic': 'info',
 'runtime.midi_reset': 'info',
 'runtime.midi_stream_events_failed': 'error',
 'runtime.native_session_legacy': 'warning',
 'runtime.native_session_ready': 'info',
 'runtime.native_session_start_failed': 'error',
 'runtime.observation_failed': 'warning',
 'runtime.output_switch_failed': 'error',
 'runtime.port_create_failed_log': 'error',
 'runtime.preset_controls_failed': 'error',
 'runtime.preset_controls_pending': 'warning',
 'runtime.priority_failed': 'error',
 'runtime.reverb_preset_apply_failed': 'error',
 'runtime.settings_recovered_fields': 'warning',
 'runtime.settings_save_failed': 'error',
 'runtime.soundfont_loaded': 'info',
 'runtime.soundfont_scan_partial': 'warning',
 'runtime.startup_cleanup_failed': 'error',
 'runtime.status_bass_init_failed': 'error',
 'runtime.status_channel_play_failed': 'error',
 'runtime.status_midi_stream_failed': 'error',
 'runtime.wasapi_disconnected': 'error',
 'runtime.wasapi_fallback': 'error',
 'runtime.wasapi_recovered': 'info',
 'runtime.wasapi_start_failed': 'error',
 'runtime.wasapi_unavailable': 'warning',
 'tempo_display.unavailable': 'warning',
 'tuning_preview.failed': 'error',
 'warning_log.clip': 'warning',
 'warning_log.clip_summary': 'warning',
 'warning_log.entry': 'warning',
 'warning_log.summary': 'warning'}

class LogEntry(str):
    def __new__(cls,text,level='error',message_id=None,*,repeat=False,detail=None,indicator=None):
        if level not in LOG_LEVELS:raise ValueError('log level')
        value=super().__new__(cls,str(text))
        value.level=level;value.message_id=message_id
        value.repeat=bool(repeat);value.detail=detail;value.indicator=indicator
        return value

def log_entry(message,level=None,*,repeat=None,detail=None,indicator=None):
    if isinstance(message,LogEntry) and all(value is None for value in (level,repeat,detail,indicator)):return message
    return LogEntry(message,level or getattr(message,'level','error'),getattr(message,'message_id',None),
        repeat=getattr(message,'repeat',False) if repeat is None else repeat,
        detail=getattr(message,'detail',None) if detail is None else detail,
        indicator=getattr(message,'indicator',None) if indicator is None else indicator)

def log_text(message_id,**values):
    return LogEntry(tr(message_id,**values),MESSAGE_LEVELS[message_id],message_id)

class LogHistory:
    def __init__(self,limit=1000):self.entries=deque(maxlen=limit)
    def extend(self,entries):
        accepted=[log_entry(entry) for entry in entries if entry not in ('STALL_DETECTED','STALL_RESUMED')]
        self.entries.extend(accepted)
        return accepted
    @staticmethod
    def accepts(entry,level,detail='summary'):
        return (LOG_LEVELS.index(entry.level)>=LOG_LEVELS.index(level)
                and (detail!='changes' or not entry.repeat))
    @staticmethod
    def display(entry,detail):return entry.detail if detail=='detailed' and entry.detail else str(entry)
    def visible(self,level,detail='summary'):
        return [self.display(entry,detail) for entry in self.entries if self.accepts(entry,level,detail)]
