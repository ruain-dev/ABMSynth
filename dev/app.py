"""Launch the sole active ABMSynth window."""

import os


def show_startup_error(error) -> None:
    """One launch-owned presentation boundary, including pre-main-window failure."""
    import sys
    import traceback
    from i18n import tr
    traceback.print_exception(error, file=sys.stderr)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        from native_widgets import NativeMessageBox
    except ImportError:
        return  # Qt unavailable: the failure remains explicit on stderr/exit1.
    try:
        application = QApplication.instance() or QApplication(sys.argv)
        prompt = NativeMessageBox()
        prompt.setWindowTitle(tr('error.startup_title'))
        prompt.setIcon(QMessageBox.Critical)
        prompt.setText(str(error))
        recover = None
        if '--recovery' not in sys.argv:
            prompt.setInformativeText(tr('support.startup_recovery_prompt'))
            recover = prompt.addButton(tr('support.recovery_start'), QMessageBox.ActionRole)
        prompt.addButton(tr('common.close'), QMessageBox.RejectRole)
        prompt.exec()
        if recover is not None and prompt.clickedButton() is recover:
            from windows_integration import spawn_restart_after_exit
            spawn_restart_after_exit(recovery=True)
    except Exception as presentation_error:
        # A broken presentation dependency must not replace the launch failure
        # or turn it into exit0. Both failures remain available on stderr.
        traceback.print_exception(presentation_error, file=sys.stderr)

# Qt's Windows DPI setup can report a benign duplicate-awareness warning when
# another frontend shares this console. Keep the terminal reserved for actual
# startup/runtime failures.
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window=false")
# Qt 6.8 changed the Windows default from GDI to DirectWrite. On the target
# display the latter visibly degrades small Korean Widget text; keep the
# verified GDI path unless a developer explicitly supplies another QPA value.
os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=gdi")


def main() -> int:
    import sys
    import tempfile
    from contextlib import ExitStack
    with ExitStack() as lifetime:
        os.environ.pop('ABMSYNTH_RECOVERY_PROFILE', None)
        os.environ.pop('ABMSYNTH_RECOVERY_ORIGINAL_INI', None)
        if '--recovery' in sys.argv:
            from pathlib import Path
            from settings import settings_path
            os.environ['ABMSYNTH_RECOVERY_ORIGINAL_INI'] = str(settings_path())
            folder = lifetime.enter_context(tempfile.TemporaryDirectory(prefix='ABMSynth-recovery-'))
            os.environ['ABMSYNTH_RECOVERY_PROFILE'] = folder
            Path(folder, 'ABMSynth.ini').write_text('[Engine]\nwindows_midi_default=0\n[UI]\nskip_driver_setup_notice=1\n', encoding='utf-8')
        try:
            return _main_session()
        finally:
            os.environ.pop('ABMSYNTH_RECOVERY_PROFILE', None)
            os.environ.pop('ABMSYNTH_RECOVERY_ORIGINAL_INI', None)


def _main_session() -> int:
    # A replacement waits for the old process to finish before importing Qt
    # or creating any app/audio resource in this new process.
    try:
        from windows_integration import run_single_application
        def start(instance):
            from product_support import apply_pending_restore
            apply_pending_restore()
            from error_reporting import start_reporting, stop_reporting
            try:
                start_reporting()
                from pyside_native_ui import run_live
                return run_live(instance=instance)
            except BaseException as error:
                from error_reporting import record
                import traceback
                record('launch_failure',traceback=''.join(traceback.format_exception(error)))
                raise
            finally:stop_reporting()
        return run_single_application(start)
    except Exception as error:
        # Settings/parsing and partial constructor failures are not all
        # RuntimeErrors. A windowed executable has no terminal fallback.
        # Leave SystemExit/KeyboardInterrupt outside this launch boundary.
        show_startup_error(error)
        return 1


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # SIGINT may arrive before Qt installs its event-loop handler.
        raise SystemExit(0)

