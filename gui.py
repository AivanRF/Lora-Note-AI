import os
import csv
import queue
import zipfile
import platform
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from datetime import datetime

import pystray
from PIL import Image, ImageDraw

from config import APP_NAME, APP_VERSION, APP_BUILD_DATE, APP_AUTHOR, Config
from logger import logger
from voice_recognition import VoiceControl
from autostart import SmartAutoStartManager
from system_launcher import SystemLauncher
from database import AppDatabase

UI_ACTIONS = {"minimize_to_tray", "restore_from_tray", "quit", "check_autostart_status"}


class VoiceControlGUI:
    """GUI. Все обращения к Tkinter — только из главного потока через очередь."""

    def __init__(self, root, start_hidden=False):
        self.root = root
        self.voice_control = None
        self.is_listening = False
        self.start_hidden = start_hidden
        self.tray_icon = None
        self.management_window = None
        self.autostart_manager = SmartAutoStartManager()

        self._ui_queue = queue.Queue()
        self._scan_cancel = threading.Event()
        self._scan_running = False
        self._closing = False

        self.setup_window()
        self.create_menu()
        self.setup_main_interface()
        self.setup_voice_control()
        self.setup_tray_icon()

        self._update_status_display()
        self._update_autostart_button()

        self.root.after(80, self._pump_ui_queue)
        self.root.after(300, self._first_run_checks)
        if self.start_hidden:
            self.root.after(600, self.minimize_to_tray)
        elif Config.APP_NAME:
            self.root.after(1500, self.start_listening_auto)

    # ================= очередь UI =================
    def _pump_ui_queue(self):
        if self._closing:
            return
        try:
            for _ in range(60):
                kind, payload = self._ui_queue.get_nowait()
                try:
                    self._dispatch(kind, payload)
                except Exception:
                    logger.exception("Ошибка обработки UI-события")
        except queue.Empty:
            pass
        finally:
            if not self._closing:
                self.root.after(80, self._pump_ui_queue)

    def _dispatch(self, kind, payload):
        if kind == "log":
            self._append_log(payload[0], payload[1])
        elif kind == "action":
            if payload == "minimize_to_tray":
                self.minimize_to_tray()
            elif payload == "restore_from_tray":
                self.restore_from_tray()
            elif payload == "quit":
                self.quit_application()
            elif payload == "check_autostart_status":
                self.check_autostart_status()
        elif kind == "status":
            if payload == "started":
                self._handle_started_listening()
            elif payload == "stopped":
                self._handle_stopped_listening()
            elif payload == "error":
                self._handle_listening_error()
            elif payload == "restart":
                self._handle_stopped_listening()
                self.root.after(400, self.start_listening)
        elif kind == "partial":
            self.partial_var.set(f"… {payload}")
        elif kind == "scan_progress":
            self._on_scan_progress(payload)

    def log_message(self, message, msg_type="info"):
        """Можно вызывать из любого потока"""
        logger.info(message)
        self._ui_queue.put(("log", (message, msg_type)))

    def _append_log(self, message, msg_type):
        if self._closing:
            return
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, f"[{timestamp}] ", "time")
        tag = msg_type if msg_type in ("command", "error", "success", "info") else "info"
        text = f"❌ {message}\n" if msg_type == "error" and not message.startswith("❌") \
            else f"{message}\n"
        self.log_text.insert(tk.END, text, tag)
        # ограничиваем размер лога, чтобы не съесть память за долгую сессию
        if int(self.log_text.index("end-1c").split(".")[0]) > 2000:
            self.log_text.delete("1.0", "500.0")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")

    # ================= окно =================
    def setup_window(self):
        self.root.title(f"{APP_NAME} — голосовое управление 🎤 ({APP_VERSION})")
        self.root.protocol("WM_DELETE_WINDOW", self.minimize_to_tray)
        width, height = 900, 680
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 2
        self.root.geometry(f"{width}x{height}+{max(x,0)}+{max(y,0)}")
        self.root.minsize(820, 600)
        self.root.resizable(True, True)
        try:
            self.root.iconbitmap(Config.resource_path("Lora.ico"))
        except Exception:
            pass

    def create_menu(self):
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Файл", menu=file_menu)
        file_menu.add_command(label="📋 Управление приложениями", command=self.manage_applications)
        file_menu.add_command(label="🔍 Сканировать программы", command=self.scan_applications)
        file_menu.add_command(label="🧹 Очистка базы", command=self.show_cleanup_menu)
        file_menu.add_separator()
        file_menu.add_command(label="📤 Экспорт логов", command=self.export_logs)
        file_menu.add_command(label="🐞 Отчёт для разработчика", command=self.build_bug_report)
        file_menu.add_separator()
        file_menu.add_command(label="📌 Свернуть в трей", command=self.minimize_to_tray)
        file_menu.add_command(label="🚪 Выход", command=self.quit_application)

        settings_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Настройки", menu=settings_menu)
        settings_menu.add_command(label="🔧 Автозапуск", command=self.toggle_autostart)
        settings_menu.add_command(label="📊 Статус автозапуска", command=self.check_autostart_status)
        settings_menu.add_separator()
        settings_menu.add_command(label="🎤 Выбор микрофона", command=self.show_mic_settings)
        self.auto_listen_var = tk.BooleanVar(value=True)
        settings_menu.add_checkbutton(label="▶️ Слушать при запуске",
                                      variable=self.auto_listen_var,
                                      command=self._toggle_auto_listen)

        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Помощь", menu=help_menu)
        help_menu.add_command(label="📖 Справка", command=self.show_help)
        help_menu.add_command(label="🎤 Голосовые команды", command=self.show_voice_commands)
        help_menu.add_command(label="📁 Открыть папку данных", command=self.open_data_folder)
        help_menu.add_separator()
        help_menu.add_command(label="ℹ️ О программе", command=self.show_about)

    def setup_main_interface(self):
        main = ttk.Frame(self.root, padding="10")
        main.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        self.setup_title_and_status(main)
        self.setup_control_buttons(main)
        self.setup_log_area(main)
        self.setup_info_panel(main)

    def setup_title_and_status(self, parent):
        frame = ttk.Frame(parent)
        frame.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=f"🎯 ГОЛОСОВОЕ УПРАВЛЕНИЕ {APP_NAME.upper()}",
                  font=("Arial", 14, "bold")).grid(row=0, column=0)
        self.status_var = tk.StringVar(value="🔴 Инициализация...")
        ttk.Label(frame, textvariable=self.status_var, font=("Arial", 9)).grid(row=1, column=0)
        self.partial_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.partial_var, font=("Arial", 9, "italic"),
                  foreground="#666").grid(row=2, column=0)

    def setup_control_buttons(self, parent):
        frame = ttk.LabelFrame(parent, text="Управление", padding="10")
        frame.grid(row=1, column=0, sticky="ew", pady=5)
        for col in range(3):
            frame.columnconfigure(col, weight=1)

        row1 = ttk.Frame(frame)
        row1.grid(row=0, column=0, columnspan=3, pady=3, sticky="ew")
        self.start_button = ttk.Button(row1, text="🎤 Начать слушать",
                                       command=self.start_listening, width=22)
        self.start_button.grid(row=0, column=0, padx=3)
        self.stop_button = ttk.Button(row1, text="⏹️ Остановить", command=self.stop_listening,
                                      width=22, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=3)
        self.autostart_button = ttk.Button(row1, text="🔧 Автозапуск",
                                           command=self.toggle_autostart, width=22)
        self.autostart_button.grid(row=0, column=2, padx=3)

        row2 = ttk.Frame(frame)
        row2.grid(row=1, column=0, columnspan=3, pady=3, sticky="ew")
        self.scan_button = ttk.Button(row2, text="🔍 Сканировать",
                                      command=self.scan_applications, width=22)
        self.scan_button.grid(row=0, column=0, padx=3)
        self.quick_scan_button = ttk.Button(row2, text="🚀 Быстрый скан",
                                            command=self.quick_scan, width=22)
        self.quick_scan_button.grid(row=0, column=1, padx=3)
        self.manage_button = ttk.Button(row2, text="📋 Приложения",
                                        command=self.manage_applications, width=22)
        self.manage_button.grid(row=0, column=2, padx=3)

        row3 = ttk.Frame(frame)
        row3.grid(row=2, column=0, columnspan=3, pady=3, sticky="ew")
        ttk.Button(row3, text="🎤 Команды", command=self.show_voice_commands,
                   width=22).grid(row=0, column=0, padx=3)
        ttk.Button(row3, text="🎧 Микрофон", command=self.show_mic_settings,
                   width=22).grid(row=0, column=1, padx=3)
        ttk.Button(row3, text="🐞 Отчёт", command=self.build_bug_report,
                   width=22).grid(row=0, column=2, padx=3)

        progress_row = ttk.Frame(frame)
        progress_row.grid(row=3, column=0, columnspan=3, pady=(6, 0), sticky="ew")
        progress_row.columnconfigure(0, weight=1)
        self.scan_progress = ttk.Progressbar(progress_row, mode="indeterminate")
        self.scan_progress.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.cancel_scan_button = ttk.Button(progress_row, text="✖ Отменить", width=12,
                                             command=self.cancel_scan, state="disabled")
        self.cancel_scan_button.grid(row=0, column=1)
        self.scan_status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.scan_status_var,
                  font=("Arial", 8)).grid(row=4, column=0, columnspan=3, sticky="w")

    def setup_log_area(self, parent):
        frame = ttk.LabelFrame(parent, text="Лог команд и событий", padding="5")
        frame.grid(row=2, column=0, sticky="nsew", pady=8)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(frame, height=14, font=("Consolas", 9),
                                state="disabled", wrap=tk.WORD)
        for tag, color in (("time", "gray"), ("command", "blue"),
                           ("error", "red"), ("success", "green"), ("info", "black")):
            self.log_text.tag_configure(tag, foreground=color)

        vbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")

        bottom = ttk.Frame(frame)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Button(bottom, text="🧽 Очистить лог", command=self.clear_log_view).pack(side=tk.LEFT)
        ttk.Button(bottom, text="📤 Экспорт логов",
                   command=self.export_logs).pack(side=tk.LEFT, padx=5)

    def setup_info_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Информация", padding="8")
        frame.grid(row=3, column=0, sticky="ew")
        info = (f"🔒 Команды начинаются со слова «Лора»   •   "
                f"🎯 Русские названия английских программ понимаются\n"
                f"💾 Данные: {Config.get_app_data_path()}\n"
                f"⚡ Версия {APP_VERSION} от {APP_BUILD_DATE} — тестовая сборка")
        ttk.Label(frame, text=info, justify=tk.LEFT, font=("Arial", 8)).grid(row=0, column=0, sticky="w")

    def clear_log_view(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state="disabled")

    # ================= голос =================
    def setup_voice_control(self):
        def gui_callback(message):
            if message in UI_ACTIONS:
                self._ui_queue.put(("action", message))
            else:
                self._ui_queue.put(("log", (message, "info")))

        def status_callback(status):
            self._ui_queue.put(("status", status))

        def partial_callback(text):
            self._ui_queue.put(("partial", text))

        try:
            self.voice_control = VoiceControl(gui_callback, status_callback, partial_callback)
            self.auto_listen_var.set(bool(self.voice_control.preferences.get("auto_listen", True)))
            if self.voice_control.is_voice_available():
                self.log_message("✅ Голосовое управление готово", "success")
            else:
                reason = self.voice_control.get_unavailable_reason()
                self.log_message(f"⚠️ Голос недоступен: {reason}", "error")
                self.log_message("ℹ️ Остальные функции (база, сканер, отчёты) работают", "info")
                self.start_button.config(state="disabled")
            self._update_status_display()
        except Exception as e:
            logger.exception("Инициализация VoiceControl")
            self.log_message(f"❌ Ошибка инициализации: {e}", "error")
            messagebox.showerror("Ошибка", f"Не удалось инициализировать ядро:\n{e}")

    def _toggle_auto_listen(self):
        if self.voice_control:
            self.voice_control.preferences.set("auto_listen", bool(self.auto_listen_var.get()))

    def start_listening_auto(self):
        if not self.voice_control or not self.voice_control.is_voice_available():
            return
        if not self.voice_control.preferences.get("auto_listen", True):
            self.log_message("ℹ️ Автопрослушивание отключено в настройках", "info")
            return
        self.start_listening()

    def start_listening(self):
        if self.is_listening or not self.voice_control:
            return
        if not self.voice_control.is_voice_available():
            messagebox.showwarning("Голос недоступен",
                                   self.voice_control.get_unavailable_reason() or "Неизвестная причина")
            return
        self.is_listening = True
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.status_var.set("🎧 Запуск прослушивания...")
        self.log_message("🎧 Начинаю прослушивание...", "info")
        threading.Thread(target=self.voice_control.start_listening,
                         name="ListenThread", daemon=True).start()

    def stop_listening(self):
        if not self.is_listening or not self.voice_control:
            return
        self.voice_control.stop_listening()
        self.is_listening = False
        self.start_button.config(state="normal")
        self.stop_button.config(state="disabled")
        self.partial_var.set("")
        self._update_status_display()
        self.log_message("⏹️ Прослушивание остановлено", "info")

    def _handle_started_listening(self):
        self.is_listening = True
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self._update_status_display()

    def _handle_stopped_listening(self):
        self.is_listening = False
        self.partial_var.set("")
        if self.voice_control and self.voice_control.is_voice_available():
            self.start_button.config(state="normal")
        self.stop_button.config(state="disabled")
        self._update_status_display()

    def _handle_listening_error(self):
        self.is_listening = False
        self.partial_var.set("")
        if self.voice_control and self.voice_control.is_voice_available():
            self.start_button.config(state="normal")
        self.stop_button.config(state="disabled")
        self.status_var.set("❌ Ошибка прослушивания — проверьте микрофон")

    def _update_status_display(self):
        autostart = "ВКЛ" if self.autostart_manager.is_autostart_enabled() else "ВЫКЛ"
        if self.is_listening:
            state = "🟢 Слушаю"
        elif self.voice_control and not self.voice_control.is_voice_available():
            state = "🟡 Голос недоступен"
        else:
            state = "⚪ Готов к работе"
        count = ""
        if self.voice_control:
            try:
                count = f" | программ в базе: {self.voice_control.database.get_database_stats()['active']}"
            except Exception:
                pass
        self.status_var.set(f"{state} | Автозапуск: {autostart}{count}")

    def _update_autostart_button(self):
        if hasattr(self, "autostart_button"):
            enabled = self.autostart_manager.is_autostart_enabled()
            self.autostart_button.config(
                text="🔧 Выкл. автозапуск" if enabled else "🔧 Вкл. автозапуск")

    def show_mic_settings(self):
        if not self.voice_control:
            messagebox.showerror("Ошибка", "Ядро не инициализировано")
            return
        AudioDeviceSelector(self.root, self.voice_control)

    # ================= сканирование =================
    def scan_applications(self):
        if not self.voice_control or self._scan_running:
            return
        self._scan_cancel.clear()
        self._scan_running = True
        self.scan_button.config(state="disabled")
        self.quick_scan_button.config(state="disabled")
        self.cancel_scan_button.config(state="normal")
        self.scan_progress.start(12)
        self.log_message("🔍 Сканирование запущено (можно отменить)", "info")
        threading.Thread(target=self._scan_worker, name="ScanThread", daemon=True).start()

    def _scan_worker(self):
        def progress(payload):
            self._ui_queue.put(("scan_progress", payload))
        try:
            self.voice_control.scan_apps_command(progress, self._scan_cancel)
        except Exception as e:
            logger.exception("Сканирование")
            self.log_message(f"❌ Ошибка сканирования: {e}", "error")
        finally:
            self._ui_queue.put(("scan_progress", {"stage": "finished"}))

    def cancel_scan(self):
        self._scan_cancel.set()
        self.scan_status_var.set("Отменяю...")

    def _on_scan_progress(self, payload):
        stage = payload.get("stage")
        if stage == "finished":
            self._scan_running = False
            self.scan_progress.stop()
            self.scan_button.config(state="normal")
            self.quick_scan_button.config(state="normal")
            self.cancel_scan_button.config(state="disabled")
            self.scan_status_var.set("")
            self._update_status_display()
            if self.management_window:
                self.management_window.refresh_all()
            return
        names = {"exe": "Поиск exe", "start_menu": "Меню Пуск",
                 "registry": "Реестр", "database": "Запись в базу", "start": "Подготовка"}
        self.scan_status_var.set(
            f"{names.get(stage, stage)}: найдено {payload.get('found', 0)} | {payload.get('current','')[:70]}")

    def quick_scan(self):
        if not self.voice_control or self._scan_running:
            return
        self.log_message("🚀 Быстрое сканирование...", "info")

        def worker():
            try:
                self.voice_control.quick_scan_command()
            finally:
                self._ui_queue.put(("scan_progress", {"stage": "finished"}))
        threading.Thread(target=worker, daemon=True).start()

    # ================= база =================
    def manage_applications(self):
        if not self.voice_control:
            return
        if self.management_window and self.management_window.exists():
            self.management_window.window.lift()
            return
        self.management_window = AppManagementWindow(self.root, self.voice_control.database,
                                                     self.voice_control)

    def show_cleanup_menu(self):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="🧹 Очистить дубликаты", command=self.cleanup_database)
        menu.add_separator()
        menu.add_command(label="🗑️ Удалить активные программы",
                         command=lambda: self.clear_database("active"))
        menu.add_command(label="🚫 Очистить чёрный список",
                         command=lambda: self.clear_database("blacklist"))
        menu.add_separator()
        menu.add_command(label="⚠️ ПОЛНАЯ ОЧИСТКА", foreground="red",
                         command=lambda: self.clear_database("all"))
        try:
            menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            menu.grab_release()

    def cleanup_database(self):
        if self.voice_control:
            self.voice_control.cleanup_database()
            self._update_status_display()

    def clear_database(self, mode):
        if not self.voice_control:
            return
        texts = {
            "all": ("Полная очистка",
                    "Удалить ВСЕ приложения, весь чёрный список и все переименования?\n"
                    "Действие необратимо."),
            "active": ("Очистка приложений",
                       "Удалить все активные приложения?\nЧёрный список останется."),
            "blacklist": ("Очистка чёрного списка",
                          "Очистить чёрный список?\nИсключённые программы снова смогут находиться."),
        }
        title, text = texts[mode]
        if not messagebox.askyesno(title, text):
            return
        if mode == "all":
            self.voice_control.clear_database_completely_command()
        elif mode == "active":
            self.voice_control.clear_active_apps_command()
        else:
            self.voice_control.clear_blacklist_command()
        self._update_status_display()
        if self.management_window:
            self.management_window.refresh_all()

    # ================= автозапуск =================
    def _first_run_checks(self):
        action = self.autostart_manager.reconcile()
        if action == "removed":
            self.log_message("🧹 Убрана запись автозапуска, оставшаяся от старой версии", "info")
        elif action == "cleaned":
            self.log_message("🧹 Автозапуск: удалены дубли старых версий", "info")
        self.autostart_manager.restore_autostart_if_needed()
        prefs = self.autostart_manager.preferences
        if prefs.get("first_run", True):
            prefs.set("first_run", False)
            answer = messagebox.askyesno(
                "Автозапуск",
                f"Запускать {APP_NAME} автоматически при входе в Windows?\n\n"
                "Программа будет стартовать свёрнутой в трей.\n"
                "Это можно изменить в любой момент в меню «Настройки».")
            if answer:
                if self.autostart_manager.enable_autostart():
                    self.log_message("✅ Автозапуск включён", "success")
                else:
                    self.log_message("❌ Не удалось включить автозапуск", "error")
            else:
                prefs.mark_user_modified_autostart(False)
                self.log_message("ℹ️ Автозапуск отключён по выбору пользователя", "info")
        self._update_status_display()
        self._update_autostart_button()

        if self.voice_control and not self.voice_control.is_voice_available():
            messagebox.showwarning(
                "Голосовое управление недоступно",
                f"{self.voice_control.get_unavailable_reason()}\n\n"
                "Интерфейс, база приложений и сканер работают.\n"
                "Для голоса нужны: pip install vosk sounddevice\n"
                "и папка модели vosk-model-small-ru-0.22 рядом с программой.")

    def toggle_autostart(self):
        if self.autostart_manager.is_autostart_enabled():
            if not messagebox.askyesno("Отключение автозапуска",
                                       "Отключить автозапуск программы?"):
                return
            success = self.autostart_manager.disable_autostart()
            self.log_message("✅ Автозапуск отключён" if success
                             else "❌ Не удалось полностью отключить автозапуск",
                             "success" if success else "error")
        else:
            success = self.autostart_manager.enable_autostart()
            self.log_message("✅ Автозапуск включён" if success
                             else "❌ Не удалось включить автозапуск",
                             "success" if success else "error")
            if not success:
                messagebox.showerror(
                    "Ошибка",
                    "Не удалось включить автозапуск.\n\n"
                    "Возможные причины:\n"
                    "• нет прав на запись в реестр/папку автозагрузки\n"
                    "• антивирус блокирует создание файлов")
        self._update_status_display()
        self._update_autostart_button()

    def check_autostart_status(self):
        status = self.autostart_manager.get_status()
        self.log_message("📊 Статус автозапуска:", "info")
        self.log_message(f"   • В системе: {'ВКЛ' if status['system'] else 'ВЫКЛ'}", "info")
        self.log_message(f"   • В настройках: {'ВКЛ' if status['prefs'] else 'ВЫКЛ'}", "info")
        self.log_message(f"   • Управление: {'пользователь' if status['user_modified'] else 'авто'}", "info")
        self.log_message(f"   • Команда: {status['command']}", "info")

    # ================= справка и отчёты =================
    def show_help(self):
        if self.voice_control:
            self.voice_control.show_help()

    def show_voice_commands(self):
        self.show_help()

    def show_about(self):
        messagebox.showinfo(
            f"О программе {APP_NAME}",
            f"{APP_NAME} — голосовое управление компьютером\n\n"
            f"Версия: {APP_VERSION}\n"
            f"Сборка от: {APP_BUILD_DATE}\n"
            f"Автор: {APP_AUTHOR}\n\n"
            f"Тестовая сборка. Движок распознавания — Vosk,\n"
            f"в будущем будет заменён собственной моделью.\n\n"
            f"Данные: {Config.get_app_data_path()}")

    def open_data_folder(self):
        path = Config.get_app_data_path()
        try:
            os.startfile(path)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть папку: {e}")

    def export_logs(self):
        try:
            default = f"{APP_NAME.replace(' ', '_')}_logs_{datetime.now():%Y%m%d_%H%M%S}.txt"
            path = filedialog.asksaveasfilename(
                title="Сохранить логи", defaultextension=".txt",
                initialfile=default, filetypes=[("Текстовый файл", "*.txt")])
            if not path:
                return
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"=== {APP_NAME} {APP_VERSION} ({APP_BUILD_DATE}) ===\n")
                f.write(f"Экспорт: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
                f.write("=" * 50 + "\n\n")
                f.write(logger.get_recent_logs(2000))
            self.log_message(f"📤 Логи сохранены: {path}", "success")
        except Exception as e:
            self.log_message(f"❌ Ошибка экспорта логов: {e}", "error")

    def build_bug_report(self):
        """ZIP для тестера: логи, настройки, профили, статистика, система"""
        try:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            target_dir = desktop if os.path.isdir(desktop) else Config.get_app_data_path()
            path = os.path.join(
                target_dir, f"{APP_NAME.replace(' ', '_')}_report_{datetime.now():%Y%m%d_%H%M%S}.zip")
            data_dir = Config.get_app_data_path()
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                logs_dir = os.path.join(data_dir, "logs")
                if os.path.isdir(logs_dir):
                    for name in os.listdir(logs_dir):
                        full = os.path.join(logs_dir, name)
                        if os.path.isfile(full):
                            z.write(full, f"logs/{name}")
                for rel in ("user_preferences.json", os.path.join("AudioProfiles", "profiles.json")):
                    full = os.path.join(data_dir, rel)
                    if os.path.isfile(full):
                        z.write(full, rel.replace("\\", "/"))
                info = [
                    f"{APP_NAME} {APP_VERSION} ({APP_BUILD_DATE})",
                    f"Дата: {datetime.now():%Y-%m-%d %H:%M:%S}",
                    f"ОС: {platform.platform()}",
                    f"Python: {platform.python_version()}",
                    f"Portable: {Config.is_portable_mode()}",
                    f"Data: {data_dir}",
                ]
                if self.voice_control:
                    info.append(f"Голос доступен: {self.voice_control.is_voice_available()}")
                    info.append(f"Причина: {self.voice_control.get_unavailable_reason()}")
                    try:
                        info.append(f"БД: {self.voice_control.database.get_database_stats()}")
                    except Exception as e:
                        info.append(f"БД: ошибка {e}")
                    try:
                        for dev in self.voice_control.get_audio_devices():
                            info.append(f"Микрофон #{dev['index']} {dev['name']} "
                                        f"[{dev['api']}] rates={dev['supported_rates']}")
                    except Exception as e:
                        info.append(f"Аудио: ошибка {e}")
                info.append(f"Автозапуск: {self.autostart_manager.get_status()}")
                z.writestr("system_info.txt", "\n".join(info))
            self.log_message(f"🐞 Отчёт готов: {path}", "success")
            messagebox.showinfo("Отчёт для разработчика",
                                f"Файл создан:\n{path}\n\nПриложите его к сообщению об ошибке.")
        except Exception as e:
            logger.exception("Создание отчёта")
            self.log_message(f"❌ Не удалось создать отчёт: {e}", "error")

    # ================= трей =================
    def setup_tray_icon(self):
        try:
            icon_path = Config.resource_path("Lora.ico")
            if os.path.exists(icon_path):
                image = Image.open(icon_path).resize((64, 64), Image.Resampling.LANCZOS)
            else:
                image = Image.new("RGB", (64, 64), "white")
                draw = ImageDraw.Draw(image)
                draw.ellipse([2, 2, 62, 62], fill="blue", outline="black")
                draw.text((26, 24), "L", fill="white")

            menu = pystray.Menu(
                pystray.MenuItem("Развернуть", lambda: self._ui_queue.put(("action", "restore_from_tray")),
                                 default=True),
                pystray.MenuItem("Слушать", lambda: self._ui_queue.put(("action", "restore_from_tray"))),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", lambda: self._ui_queue.put(("action", "quit"))),
            )
            self.tray_icon = pystray.Icon("LoraAI", image, APP_NAME, menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception as e:
            logger.error(f"Трей недоступен: {e}")
            self.root.protocol("WM_DELETE_WINDOW", self.quit_application)

    def minimize_to_tray(self):
        if self.tray_icon is None:
            self.root.iconify()
            return
        self.root.withdraw()
        self.log_message("📌 Свёрнуто в трей. Голос продолжает работать.", "info")

    def restore_from_tray(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._update_status_display()
        self.log_message("📱 Окно развёрнуто из трея", "info")

    def quit_application(self):
        if self._closing:
            return
        self._closing = True
        self._scan_cancel.set()
        try:
            if self.voice_control:
                self.voice_control.stop_listening()
        except Exception:
            pass
        try:
            if self.tray_icon:
                self.tray_icon.stop()
        except Exception:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass


class AudioDeviceSelector:
    """Рабочий выбор микрофона"""

    def __init__(self, parent, voice_control):
        self.voice_control = voice_control
        self.devices = []
        self.window = tk.Toplevel(parent)
        self.window.title("🎤 Выбор микрофона")
        self.window.geometry("760x440")
        self.window.transient(parent)

        ttk.Label(self.window, text="Выберите микрофон для распознавания",
                  font=("Arial", 12, "bold")).pack(pady=8)

        frame = ttk.Frame(self.window)
        frame.pack(fill=tk.BOTH, expand=True, padx=10)
        columns = ("index", "name", "api", "rate")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=12)
        for col, title, width in (("index", "№", 50), ("name", "Устройство", 380),
                                  ("api", "API", 120), ("rate", "Частота", 90)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width)
        vbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.hint_var = tk.StringVar(value="")
        ttk.Label(self.window, textvariable=self.hint_var,
                  font=("Arial", 8)).pack(pady=4)

        buttons = ttk.Frame(self.window)
        buttons.pack(pady=8)
        ttk.Button(buttons, text="✅ Применить", command=self.apply).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="🔄 Обновить", command=self.refresh).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="🧪 Проверить", command=self.test_device).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="❌ Закрыть", command=self.window.destroy).pack(side=tk.LEFT, padx=4)

        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.devices = self.voice_control.get_audio_devices()
        saved = self.voice_control.preferences.get("selected_device_index")
        for dev in self.devices:
            mark = " ⭐" if dev["index"] == saved else (" (по умолчанию)" if dev["is_default"] else "")
            self.tree.insert("", "end", values=(dev["index"], dev["name"] + mark,
                                                dev["api"], f"{dev['rate']} Hz"))
        self.hint_var.set(f"Найдено устройств: {len(self.devices)}. "
                          f"⭐ — сохранённый выбор." if self.devices
                          else "Микрофоны не найдены. Проверьте подключение и драйверы.")

    def _selected(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning("Внимание", "Выберите устройство из списка", parent=self.window)
            return None
        index = int(self.tree.item(selection[0])["values"][0])
        return next((d for d in self.devices if d["index"] == index), None)

    def apply(self):
        dev = self._selected()
        if not dev:
            return
        self.voice_control.set_audio_device(dev["index"], dev["rate"])
        self.window.destroy()

    def test_device(self):
        dev = self._selected()
        if not dev:
            return
        if not dev["supported_rates"]:
            messagebox.showerror("Проверка", f"Устройство не поддерживает тестовые частоты.\n"
                                             f"Возможно, оно занято другим приложением.",
                                 parent=self.window)
            return
        messagebox.showinfo("Проверка",
                            f"{dev['name']}\nAPI: {dev['api']}\n"
                            f"Поддерживаемые частоты: {dev['supported_rates']}\n"
                            f"Каналов: {dev['channels']}\n\n"
                            f"Устройство готово к работе.", parent=self.window)


class AppManagementWindow:
    """Управление приложениями: переименование, чёрный список, экспорт"""

    def __init__(self, parent, database, voice_control):
        self.database = database
        self.voice_control = voice_control
        self.window = tk.Toplevel(parent)
        self.window.title("📋 Управление приложениями")
        self.window.geometry("1050x720")
        self.window.transient(parent)
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(1, weight=1)
        self._build()
        self.refresh_all()

    def exists(self):
        try:
            return bool(self.window.winfo_exists())
        except Exception:
            return False

    def _build(self):
        top = ttk.Frame(self.window, padding="8")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="🔎 Поиск:").grid(row=0, column=0, padx=(0, 5))
        self.search_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.search_var)
        entry.grid(row=0, column=1, sticky="ew")
        entry.bind("<KeyRelease>", lambda e: self.refresh_list())
        ttk.Button(top, text="🔄 Обновить", command=self.refresh_all).grid(row=0, column=2, padx=5)

        tabs = ttk.Notebook(self.window)
        tabs.grid(row=1, column=0, sticky="nsew", padx=8)
        active_tab = ttk.Frame(tabs)
        blacklist_tab = ttk.Frame(tabs)
        tabs.add(active_tab, text="📋 Активные приложения")
        tabs.add(blacklist_tab, text="🚫 Чёрный список")
        self._setup_active_tab(active_tab)
        self._setup_blacklist_tab(blacklist_tab)

        buttons = ttk.Frame(self.window, padding="8")
        buttons.grid(row=2, column=0, sticky="ew")
        for text, command in (
                ("✏️ Переименовать", self.rename_app),
                ("🚫 В чёрный список", self.delete_to_blacklist),
                ("▶️ Запустить", self.test_app),
                ("📤 Экспорт CSV", self.export_apps),
                ("❌ Закрыть", self.window.destroy),
        ):
            ttk.Button(buttons, text=text, command=command, width=20).pack(side=tk.LEFT, padx=3)

    def _setup_active_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        columns = ("original", "custom", "path")
        self.tree = ttk.Treeview(parent, columns=columns, show="headings")
        for col, title, width in (("original", "Оригинальное название", 260),
                                  ("custom", "Своё название", 220),
                                  ("path", "Путь", 500)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, minwidth=120)
        vbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.tree.yview)
        hbar = ttk.Scrollbar(parent, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Double-1>", lambda e: self.rename_app())

    def _setup_blacklist_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        columns = ("original", "path", "reason", "date")
        self.blacklist_tree = ttk.Treeview(parent, columns=columns, show="headings")
        for col, title, width in (("original", "Название", 220), ("path", "Путь", 420),
                                  ("reason", "Причина", 180), ("date", "Дата", 140)):
            self.blacklist_tree.heading(col, text=title)
            self.blacklist_tree.column(col, width=width, minwidth=100)
        vbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.blacklist_tree.yview)
        self.blacklist_tree.configure(yscrollcommand=vbar.set)
        self.blacklist_tree.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        buttons = ttk.Frame(parent)
        buttons.grid(row=1, column=0, pady=6, sticky="w")
        ttk.Button(buttons, text="✅ Восстановить",
                   command=self.restore_from_blacklist).pack(side=tk.LEFT, padx=3)
        ttk.Button(buttons, text="🗑️ Убрать из списка",
                   command=self.remove_from_blacklist_permanently).pack(side=tk.LEFT, padx=3)

    def refresh_all(self):
        if not self.exists():
            return
        self.refresh_list()
        self.refresh_blacklist()

    def refresh_list(self):
        if not self.exists():
            return
        self.tree.delete(*self.tree.get_children())
        needle = self.search_var.get().lower().strip()
        for app in self.database.get_all_applications():
            name = app[2] or app[1]
            if needle and needle not in name.lower() and needle not in app[3].lower():
                continue
            self.tree.insert("", "end", values=(app[1], app[2] or "(не задано)", app[3]))

    def refresh_blacklist(self):
        if not self.exists():
            return
        self.blacklist_tree.delete(*self.blacklist_tree.get_children())
        for item in self.database.get_blacklist():
            self.blacklist_tree.insert("", "end", values=(item[2], item[1], item[3], item[4]))

    def get_selected_app(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning("Внимание", "Выберите приложение", parent=self.window)
            return None
        values = self.tree.item(selection[0])["values"]
        return {"original_name": values[0],
                "custom_name": values[1] if values[1] != "(не задано)" else None,
                "exe_path": values[2]}

    def get_selected_blacklist_item(self):
        selection = self.blacklist_tree.selection()
        if not selection:
            messagebox.showwarning("Внимание", "Выберите запись", parent=self.window)
            return None
        values = self.blacklist_tree.item(selection[0])["values"]
        return {"original_name": values[0], "exe_path": values[1]}

    def rename_app(self):
        app = self.get_selected_app()
        if not app:
            return
        new_name = simpledialog.askstring(
            "Переименование", f"Новое название для «{app['original_name']}»:",
            initialvalue=app["custom_name"] or app["original_name"], parent=self.window)
        if not new_name or not new_name.strip():
            return
        try:
            self.database.update_custom_name(app["exe_path"], new_name.strip())
            self.refresh_list()
            self.voice_control._show_message(f"✅ Переименовано: {new_name.strip()}")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e), parent=self.window)

    def delete_to_blacklist(self):
        app = self.get_selected_app()
        if not app:
            return
        display = app["custom_name"] or app["original_name"]
        if not messagebox.askyesno("Чёрный список",
                                   f"Скрыть «{display}»?\nОно не будет находиться при сканировании.",
                                   parent=self.window):
            return
        if self.database.add_to_blacklist(app["exe_path"], app["original_name"]):
            self.database.delete_application(app["exe_path"])
            self.refresh_all()

    def test_app(self):
        app = self.get_selected_app()
        if not app:
            return
        display = app["custom_name"] or app["original_name"]
        ok, error = SystemLauncher.launch_path(app["exe_path"])
        if ok:
            self.database.update_last_used(app["exe_path"])
            messagebox.showinfo("Запуск", f"«{display}» запускается", parent=self.window)
        else:
            if error == "Файл не найден":
                if messagebox.askyesno("Файл не найден",
                                       f"{app['exe_path']}\n\nУдалить запись из базы?",
                                       parent=self.window):
                    self.database.delete_application(app["exe_path"])
                    self.refresh_list()
            else:
                messagebox.showerror("Ошибка", error, parent=self.window)

    def export_apps(self):
        path = filedialog.asksaveasfilename(
            title="Экспорт списка приложений", defaultextension=".csv",
            initialfile="lora_apps.csv", filetypes=[("CSV", "*.csv")], parent=self.window)
        if not path:
            return
        try:
            count = self.database.export_to_csv(path)
            messagebox.showinfo("Экспорт", f"Выгружено {count} записей:\n{path}", parent=self.window)
        except Exception as e:
            messagebox.showerror("Ошибка", str(e), parent=self.window)

    def restore_from_blacklist(self):
        item = self.get_selected_blacklist_item()
        if not item:
            return
        if self.database.remove_from_blacklist(item["exe_path"]):
            self.refresh_blacklist()
            messagebox.showinfo("Готово",
                                f"«{item['original_name']}» убрано из чёрного списка.\n"
                                f"Запустите сканирование, чтобы вернуть его в базу.",
                                parent=self.window)

    def remove_from_blacklist_permanently(self):
        self.restore_from_blacklist()