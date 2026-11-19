import os
import sys
import ctypes
import threading
import traceback
from datetime import datetime

from config import APP_NAME, APP_VERSION, Config


def hide_console():
    if os.name != "nt":
        return
    try:
        window = ctypes.windll.kernel32.GetConsoleWindow()
        if window:
            ctypes.windll.user32.ShowWindow(window, 0)
    except Exception:
        pass


def enable_dpi_awareness():
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_lock_handle = None


def acquire_single_instance():
    """Не даём запустить две копии — иначе два микрофона и две базы"""
    global _lock_handle
    if os.name != "nt":
        return True
    try:
        import msvcrt
        path = os.path.join(Config.get_app_data_path(), "app.lock")
        _lock_handle = open(path, "w")
        msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except Exception:
        return False


def install_crash_hooks(logger):
    def handle(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.error(f"НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ:\n{text}")
        try:
            from tkinter import messagebox
            messagebox.showerror(
                f"{APP_NAME} — ошибка",
                f"{exc_value}\n\nПодробности в логе.\n"
                f"Соберите отчёт: Файл → Отчёт для разработчика.")
        except Exception:
            pass

    def thread_handle(args):
        logger.error(f"ИСКЛЮЧЕНИЕ В ПОТОКЕ {args.thread}:\n"
                     + "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = handle
    if hasattr(threading, "excepthook"):
        threading.excepthook = thread_handle


def main():
    start_hidden = "--hidden" in sys.argv or "--minimized" in sys.argv
    if start_hidden:
        hide_console()
    enable_dpi_awareness()

    Config.ensure_directories()
    from logger import logger
    install_crash_hooks(logger)

    logger.info("=" * 60)
    logger.info(f"{APP_NAME} {APP_VERSION} — запуск")
    logger.info(f"Время: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"Аргументы: {sys.argv}")
    logger.info(f"Данные: {Config.get_app_data_path()}")
    logger.info(f"Portable: {Config.is_portable_mode()}")

    import tkinter as tk
    from tkinter import messagebox

    if not acquire_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, "Программа уже запущена.\nПроверьте значок в трее.")
        root.destroy()
        return

    from gui import VoiceControlGUI
    root = tk.Tk()
    app = VoiceControlGUI(root, start_hidden=start_hidden)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.quit_application()
    finally:
        logger.info("Завершение работы")


if __name__ == "__main__":
    main()