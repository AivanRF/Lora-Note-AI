import os
import json
import time
import queue
import ctypes
import threading

from config import SAMPLE_RATE, CHUNK_SIZE, MODEL_PATH
from logger import logger
from database import AppDatabase
from app_scanner import AppScanner
from user_preferences import UserPreferences
from utils import AdvancedTranslationSystem, SecureVoiceCommandExtractor
from audio_config import AudioProfileManager
from system_launcher import SystemLauncher

# Опциональные зависимости: без них GUI и база продолжают работают
try:
    import sounddevice as sd
except Exception as e:
    sd = None
    logger.warning(f"sounddevice недоступен: {e}")

try:
    import vosk
    vosk.SetLogLevel(-1)
except Exception as e:
    vosk = None
    logger.warning(f"vosk недоступен: {e}")


class AudioDeviceManager:
    """Подбор микрофона. Частоты проверяются без реальной записи."""

    TEST_RATES = [16000, 48000, 44100, 32000, 22050, 11025, 8000]

    def __init__(self, sample_rate=16000, chunk_size=4000):
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.selected_device = None
        self.profile_manager = AudioProfileManager()

    @staticmethod
    def is_available():
        return sd is not None

    def _hostapi_name(self, index):
        try:
            return sd.query_hostapis(index)["name"]
        except Exception:
            return "unknown"

    def _supported_rates(self, device_index, default_rate):
        supported = []
        for rate in self.TEST_RATES:
            try:
                sd.check_input_settings(device=device_index, samplerate=rate,
                                        channels=1, dtype="int16")
                supported.append(rate)
            except Exception:
                continue
        default_rate = int(default_rate or 44100)
        if default_rate not in supported:
            try:
                sd.check_input_settings(device=device_index, samplerate=default_rate,
                                        channels=1, dtype="int16")
                supported.append(default_rate)
            except Exception:
                pass
        return supported

    def list_input_devices(self):
        """Список микрофонов для GUI"""
        if sd is None:
            return []
        devices = []
        try:
            for index, dev in enumerate(sd.query_devices()):
                if dev.get("max_input_channels", 0) <= 0:
                    continue
                rates = self._supported_rates(index, dev.get("default_samplerate"))
                devices.append({
                    "index": index,
                    "name": dev["name"],
                    "api": self._hostapi_name(dev["hostapi"]),
                    "hostapi": dev["hostapi"],
                    "rate": rates[0] if rates else int(dev.get("default_samplerate", 44100)),
                    "supported_rates": rates,
                    "channels": dev["max_input_channels"],
                    "default_samplerate": dev.get("default_samplerate", 44100),
                    "is_default": index == self._default_input_index(),
                })
        except Exception as e:
            logger.error(f"list_input_devices: {e}")
        return devices

    @staticmethod
    def _default_input_index():
        try:
            return sd.default.device[0]
        except Exception:
            return -1

    def find_best_audio_device(self, preferred_index=None):
        if sd is None:
            return None
        devices = self.list_input_devices()
        if not devices:
            return None

        if preferred_index is not None:
            for dev in devices:
                if dev["index"] == preferred_index:
                    params = self.profile_manager.get_best_params_for_device(
                        dev, dev["supported_rates"])
                    dev["working_params"] = params
                    dev["from_profile"] = True
                    self.selected_device = dev
                    logger.info(f"Микрофон выбран пользователем: {dev['name']}")
                    return dev

        candidates = []
        for dev in devices:
            profile = self.profile_manager.get_device_profile(dev)
            if profile and self.profile_manager._is_profile_valid(profile):
                dev["working_params"] = dict(profile["working_params"])
                dev["from_profile"] = True
                dev["success_count"] = profile.get("success_count", 0)
                dev["failure_count"] = profile.get("failure_count", 0)
            else:
                dev["working_params"] = self.profile_manager._find_best_params(
                    dev, dev["supported_rates"])
                dev["from_profile"] = False
                dev["success_count"] = 0
                dev["failure_count"] = 0
            candidates.append(dev)

        def score(dev):
            value = 0
            if dev["is_default"]:
                value += 120
            if dev["from_profile"]:
                value += 60 + min(dev["success_count"], 20) * 2
                value -= min(dev["failure_count"], 10) * 5
            if dev["working_params"]["sample_rate"] == 16000:
                value += 80
            api = dev["api"].lower()
            if "wasapi" in api:
                value += 25
            elif "mme" in api:
                value += 5
            if dev["working_params"].get("latency") == "low":
                value += 10
            return value

        best = max(candidates, key=score)
        self.selected_device = best
        logger.info(f"Микрофон: #{best['index']} {best['name']} "
                    f"({best['api']}, {best['working_params']['sample_rate']} Hz)")
        return best

    def get_audio_stream_config(self):
        if not self.selected_device:
            return None
        params = self.selected_device["working_params"]
        return {
            "device": self.selected_device["index"],
            "samplerate": params["sample_rate"],
            "channels": params.get("channels", 1),
            "blocksize": params.get("blocksize", 1024),
            "dtype": params.get("dtype", "int16"),
            "latency": params.get("latency", "low"),
        }

    def mark_success(self):
        if self.selected_device:
            self.profile_manager.save_device_profile(
                self.selected_device, self.selected_device["working_params"], success=True)

    def mark_failure(self):
        if self.selected_device:
            self.profile_manager.save_device_profile(
                self.selected_device, self.selected_device["working_params"], success=False)

    def print_device_info(self):
        if not self.selected_device:
            return
        dev = self.selected_device
        params = dev["working_params"]
        logger.info(f"Устройство #{dev['index']} '{dev['name']}' | {dev['api']} | "
                    f"{params['sample_rate']} Hz | каналов {params.get('channels',1)} | "
                    f"блок {params.get('blocksize')}")


class AdaptiveAudioStream:
    """Создание потока с адаптацией параметров при ошибках PortAudio"""

    def __init__(self, device_manager, max_retries=4):
        self.device_manager = device_manager
        self.max_retries = max_retries
        self.stream = None

    def create_stream(self, callback):
        device = self.device_manager.selected_device
        if not device:
            raise RuntimeError("Аудиоустройство не выбрано")
        params = dict(device["working_params"])
        last_error = None

        for attempt in range(self.max_retries):
            try:
                logger.info(f"Поток: попытка {attempt+1}, параметры {params}")
                self.stream = sd.RawInputStream(
                    samplerate=params["sample_rate"],
                    blocksize=params["blocksize"],
                    device=device["index"],
                    dtype=params["dtype"],
                    channels=params["channels"],
                    callback=callback,
                    latency=params["latency"],
                )
                device["working_params"] = params
                return self.stream
            except Exception as e:
                last_error = e
                logger.warning(f"Поток не создан: {e}")
                params = self._adapt_parameters(params, str(e), attempt)

        self.device_manager.mark_failure()
        raise RuntimeError(f"Не удалось открыть микрофон: {last_error}")

    def _adapt_parameters(self, params, error_msg, attempt):
        new = dict(params)
        low = error_msg.lower()
        if "sample rate" in low or "samplerate" in low or "frequency" in low:
            rates = [16000, 48000, 44100, 32000, 22050, 8000]
            current = new["sample_rate"]
            new["sample_rate"] = next((r for r in rates if r != current), 44100)
        elif "channel" in low:
            new["channels"] = 1 if new.get("channels", 1) != 1 else 2
        elif "buffer" in low or "blocksize" in low or "frames" in low:
            new["blocksize"] = min(new.get("blocksize", 1024) * 2, 8192)
        else:
            # общий сценарий: повышаем стабильность
            if attempt == 0:
                new["latency"] = "high"
            elif attempt == 1:
                new["blocksize"] = 0        # пусть PortAudio решает сам
                new["latency"] = "high"
            else:
                new["sample_rate"] = 48000 if new["sample_rate"] != 48000 else 44100
                new["blocksize"] = 2048
                new["latency"] = "high"
        return new


class VoiceControl:
    """Голосовое управление: распознавание в воркере, команды через GUI-колбэки"""

    ALIASES = {
        "открой звук": "включить звук",
        "закрой звук": "выключить звук",
        "выключи звук": "выключить звук",
        "включи звук": "включить звук",
        "отключи звук": "выключить звук",
        "убери звук": "выключить звук",
        "верни звук": "включить звук",
        "следующий трек": "следующая песня",
        "следующая": "следующая песня",
        "предыдущая песня": "предыдущая песня",
        "предыдущий трек": "предыдущая песня",
        "продолжи": "продолжить",
        "продолжай": "продолжить",
        "стоп": "пауза",
        "покажи программу": "покажи программы",
        "покажи список программ": "покажи программы",
        "сканируй": "сканируй программы",
        "помощь": "справка",
        "пока": "пока лора",
        "выход": "пока лора",
        "завершить выполнение": "пока лора",
        "сверни": "свернись",
        "сверни окно": "свернись",
        "покажи окно": "развернись",
        "развернись": "развернись",
        "вернись": "развернись",
    }

    def __init__(self, gui_callback=None, status_callback=None, partial_callback=None):
        self.gui_callback = gui_callback
        self.status_callback = status_callback
        self.partial_callback = partial_callback

        self.model = None
        self.recognizer = None
        self.model_error = None
        self.sample_rate = SAMPLE_RATE
        self.chunk_size = CHUNK_SIZE

        self.is_listening = False
        self.audio_stream = None
        self.device_manager = None
        self._worker = None
        self._queue = queue.Queue(maxsize=300)
        self._restart_requested = False
        self._overflow_warned = 0
        self._error_count = 0

        self.preferences = UserPreferences()
        self.command_extractor = SecureVoiceCommandExtractor()
        self.database = AppDatabase()
        self.app_scanner = AppScanner(self.database)
        self.translation_system = AdvancedTranslationSystem()

        self._initialize_commands()
        self.initialize_model()

    # ---------- инициализация ----------
    def _initialize_commands(self):
        self.commands = {
            "выключить звук": self.mute_volume,
            "включить звук": self.unmute_volume,
            "следующая песня": self.next_track,
            "предыдущая песня": self.previous_track,
            "пауза": self.pause_media,
            "продолжить": self.play_media,
            "громче": self.volume_up,
            "тише": self.volume_down,
            "пока лора": self.exit_program,
            "привет лора": self.hello_lora,
            "привет": self.hello_lora,
            "свернись": self.minimize_to_tray_command,
            "свернись в трей": self.minimize_to_tray_command,
            "скройся": self.minimize_to_tray_command,
            "в трей": self.minimize_to_tray_command,
            "развернись": self.restore_window_command,
            "сканируй программы": self.scan_apps_command,
            "покажи программы": self.show_apps_command,
            "быстрое сканирование": self.quick_scan_command,
            "популярные программы": self.show_popular_apps,
            "статистика": self.show_stats,
            "справка": self.show_help,
            "очисти базу": self.cleanup_database,
            "статус автозапуска": self.show_autostart_status,
        }

    def initialize_model(self):
        """Загрузка модели. Никогда не бросает исключение — GUI должен жить."""
        if vosk is None:
            self.model_error = "Библиотека vosk не установлена (pip install vosk)"
            logger.error(self.model_error)
            return False
        path = MODEL_PATH
        if not os.path.isdir(path):
            self.model_error = f"Модель не найдена: {path}"
            logger.error(self.model_error)
            return False
        try:
            self.model = vosk.Model(path)
            self.recognizer = vosk.KaldiRecognizer(self.model, float(self.sample_rate))
            logger.info(f"Модель Vosk загружена: {path}")
            return True
        except Exception as e:
            self.model_error = f"Ошибка загрузки модели: {e}"
            logger.exception("Загрузка модели Vosk")
            return False

    def is_voice_available(self):
        return bool(self.model and self.recognizer and sd is not None)

    def get_unavailable_reason(self):
        if sd is None:
            return "Библиотека sounddevice не установлена (pip install sounddevice)"
        if self.model_error:
            return self.model_error
        return None

    # ---------- поток ----------
    def start_listening(self):
        if self.is_listening:
            return
        if not self.is_voice_available():
            self._show_error(f"❌ Голос недоступен: {self.get_unavailable_reason()}")
            self._emit_status("error")
            return

        self._drain_queue()
        try:
            self._show_message("🔍 Определяю микрофон...")
            self.device_manager = AudioDeviceManager(self.sample_rate, self.chunk_size)
            device = self.device_manager.find_best_audio_device(
                self.preferences.get("selected_device_index"))
            if not device:
                self._show_error("❌ Не найдено ни одного микрофона. Подключите устройство.")
                self._emit_status("error")
                return
            self.device_manager.print_device_info()

            new_rate = device["working_params"]["sample_rate"]
            if new_rate != self.sample_rate or self.recognizer is None:
                self.sample_rate = new_rate
                self.recognizer = vosk.KaldiRecognizer(self.model, float(new_rate))
                logger.info(f"Распознаватель: {new_rate} Hz")

            adapter = AdaptiveAudioStream(self.device_manager)
            self.audio_stream = adapter.create_stream(self.audio_callback)
            self.is_listening = True
            self._worker = threading.Thread(target=self._processing_loop,
                                            name="VoskWorker", daemon=True)
            self._worker.start()
            self.audio_stream.start()
            self.device_manager.mark_success()
            self._show_message(f"✅ Слушаю. Микрофон: {device['name']}")
            self._emit_status("started")

            while self.is_listening:
                time.sleep(0.05)
        except Exception as e:
            logger.exception("Ошибка прослушивания")
            self._show_error(f"❌ Ошибка прослушивания: {e}")
            self._emit_status("error")
        finally:
            self.is_listening = False
            self.stop_audio_stream()
            self._drain_queue()
            if self._restart_requested:
                self._restart_requested = False
                self._emit_status("restart")
            else:
                self._emit_status("stopped")

    def stop_listening(self):
        self.is_listening = False
        self.stop_audio_stream()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def stop_audio_stream(self):
        stream, self.audio_stream = self.audio_stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def _drain_queue(self):
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def audio_callback(self, indata, frames, time_info, status):
        """Максимально дешёвый callback — только очередь"""
        if not self.is_listening:
            return
        if status and self._overflow_warned < 5:
            self._overflow_warned += 1
            logger.warning(f"Аудиостатус: {status}")
        try:
            self._queue.put_nowait(bytes(indata))
        except queue.Full:
            pass  # лучше потерять кадр, чем заблокировать PortAudio

    def _processing_loop(self):
        while self.is_listening:
            try:
                data = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if data is None:
                break
            recognizer = self.recognizer
            if recognizer is None:
                continue
            try:
                if recognizer.AcceptWaveform(data):
                    text = json.loads(recognizer.Result()).get("text", "").strip()
                    if text:
                        self.process_command(text)
                elif self.partial_callback:
                    partial = json.loads(recognizer.PartialResult()).get("partial", "")
                    if partial:
                        self.partial_callback(partial)
            except Exception as e:
                logger.exception("Ошибка распознавания")
                self._error_count += 1
                if self._error_count <= 3:
                    self._show_error(f"❌ Сбой обработки команды: {e}")
                elif self._error_count == 4:
                    self._show_error("❌ Повторяющиеся сбои. Соберите отчёт: Файл → Отчёт для разработчика")

    # ---------- команды ----------
    def process_command(self, text):
        low = text.lower().strip()
        self._show_message(f"🎤 Услышано: «{text}»")
        if not (self.command_extractor.contains_required_keyword(low)
                or self._is_unsecured_system_command(low)):
            self._show_message("🔒 Отклонено: нет слова «Лора»")
            return False
        command, confidence = self.command_extractor.extract_command(low)
        if not command:
            return False
        self._show_message(f"🎯 Команда: «{command}» ({int(confidence*100)}%)")
        return self._execute_command_safely(command, low)

    def _is_unsecured_system_command(self, text):
        return self.command_extractor.is_unsecured_command(text)

    def _contains_lora_word(self, text):
        return self.command_extractor.contains_required_keyword(text)

    def _execute_command_safely(self, command, original_text):
        try:
            command = self.ALIASES.get(command, command)

            handler = self.commands.get(command)
            if handler:
                handler()
                return True

            for key, action in self.commands.items():
                if command.startswith(key + " "):
                    action()
                    return True

            if command.startswith("открой "):
                return self._handle_open(command[7:].strip())
            if command.startswith("закрой "):
                return self._handle_close(command[7:].strip())
            if command.startswith("покажи "):
                rest = command[7:].strip()
                if "программ" in rest:
                    self.show_apps_command()
                    return True
                return self._handle_open(rest)

            if SystemLauncher.is_system_app(command):
                ok, message = SystemLauncher.launch_system_app(command)
                if message:
                    self._show_message(message)
                if ok:
                    return True

            return self._handle_open(command)
        except Exception as e:
            logger.exception(f"Выполнение команды '{command}'")
            self._show_error(f"❌ Ошибка выполнения: {e}")
            return False

    def _handle_open(self, name):
        if not name:
            return False
        system = SystemLauncher.match(name)
        if system:
            ok, message = SystemLauncher.launch_system_app(name)
            if message:
                self._show_message(message)
            if ok:
                return True
        return self.open_application_by_name(name)

    def _handle_close(self, name):
        if not name:
            return False
        app = self.database.get_application_by_name(name)
        if not app:
            self._show_message(f"❌ Не знаю, как закрыть «{name}»")
            return False
        display = app[2] or app[1]
        ok, error = SystemLauncher.close_by_exe_path(app[3])
        if ok:
            self._show_message(f"🛑 Закрываю {display}")
            return True
        self._show_message(f"⚠️ Не удалось закрыть {display}: {error}")
        return False

    def open_application_by_name(self, app_name):
        app = self.database.get_application_by_name(app_name)
        if not app:
            self._show_message(f"❌ «{app_name}» не найдена в базе")
            self._show_similar_apps(app_name)
            return False
        display = app[2] or app[1]
        return self.launch_application(app[3], display)

    def launch_application(self, exe_path, app_name):
        ok, error = SystemLauncher.launch_path(exe_path)
        if ok:
            self._show_message(f"🚀 Запускаю {app_name}")
            self.database.update_last_used(exe_path)
            return True
        self._show_error(f"❌ Не удалось запустить {app_name}: {error}")
        return False

    def _process_clear_command(self, text):
        """Совместимость: подсказки по нераспознанной команде"""
        self._show_similar_apps(text)
        return False

    def get_all_commands(self):
        """Только статические команды. Динамические по именам приложений убраны:
        они пересобирались на каждую фразу и запускали случайные программы."""
        return dict(self.commands)

    def find_application_fuzzy(self, spoken_name):
        app = self.database.get_application_by_name(spoken_name)
        return (app[2] or app[1]) if app else None

    def _show_similar_apps(self, search_term):
        matches = self.database.search_applications(search_term, limit=3)
        if matches:
            self._show_message("💡 Возможно, вы имели в виду:")
            for row, score in matches:
                self._show_message(f"   • {row[2] or row[1]} ({int(score*100)}%)")
        else:
            self._show_message("📋 База пуста или совпадений нет. "
                               "Скажите «Лора, сканируй программы»")

    # ---------- микрофон ----------
    def set_audio_device(self, index, rate=None):
        self.preferences.set("selected_device_index", int(index))
        if rate:
            self.preferences.set("selected_sample_rate", int(rate))
        self._show_message(f"🎤 Микрофон сохранён (#{index}). Перезапускаю прослушивание...")
        if self.is_listening:
            self._restart_requested = True
            self.stop_listening()

    def get_audio_devices(self):
        return AudioDeviceManager().list_input_devices()

    # ---------- системные действия ----------
    @staticmethod
    def _press_vk(vk):
        if os.name != "nt":
            return
        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 0x0002, 0)   # KEYEVENTF_KEYUP — раньше не отправлялся

    def _set_mute(self, mute):
        """Точный мьют через pycaw. False = недоступен, сработала клавиша-переключатель."""
        try:
            import comtypes
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        except Exception:
            self._press_vk(0xAD)
            return False

        initialized = False
        try:
            # COM нужно поднимать в том потоке, где вызываем (VoskWorker)
            try:
                comtypes.CoInitialize()
                initialized = True
            except OSError:
                pass  # поток уже инициализирован — это не ошибка
            device = AudioUtilities.GetSpeakers()
            interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            volume.SetMute(1 if mute else 0, None)
            return True
        except Exception as e:
            logger.warning(f"pycaw не сработал ({e}), использую клавишу mute")
            self._press_vk(0xAD)
            return False
        finally:
            if initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass

    def mute_volume(self):
        exact = self._set_mute(True)
        self._show_message("🔇 Звук выключен" if exact
                           else "🔇 Звук переключён (точное состояние недоступно)")

    def unmute_volume(self):
        exact = self._set_mute(False)
        self._show_message("🔊 Звук включён" if exact
                           else "🔊 Звук переключён (точное состояние недоступно)")

    def next_track(self):
        self._press_vk(0xB0)
        self._show_message("⏭️ Следующий трек")

    def previous_track(self):
        self._press_vk(0xB1)
        self._show_message("⏮️ Предыдущий трек")

    def pause_media(self):
        self._press_vk(0xB3)
        self._show_message("⏸️ Пауза / воспроизведение")

    def play_media(self):
        self._press_vk(0xB3)
        self._show_message("▶️ Пауза / воспроизведение")

    def volume_up(self):
        for _ in range(2):
            self._press_vk(0xAF)
        self._show_message("🔊 Громче")

    def volume_down(self):
        for _ in range(2):
            self._press_vk(0xAE)
        self._show_message("🔉 Тише")

    # ---------- команды интерфейса ----------
    def minimize_to_tray_command(self):
        self._show_message("📌 Сворачиваюсь в трей")
        if self.gui_callback:
            self.gui_callback("minimize_to_tray")

    def restore_window_command(self):
        if self.gui_callback:
            self.gui_callback("restore_from_tray")

    def exit_program(self):
        self._show_message("👋 Пока, до встречи!")
        self.is_listening = False
        if self.gui_callback:
            self.gui_callback("quit")

    def show_autostart_status(self):
        if self.gui_callback:
            self.gui_callback("check_autostart_status")

    def hello_lora(self):
        self._show_message("Привет! Я Лора. Готова к работе 🎯")

    # ---------- база ----------
    def scan_apps_command(self, progress=None, cancel_event=None):
        self._show_message("🔍 Сканирую систему...")
        result = self.app_scanner.populate_database(progress, cancel_event)
        state = "прервано" if result["cancelled"] else "завершено"
        self._show_message(f"✅ Сканирование {state} за {result['elapsed']} c")
        self._show_message(f"📊 Добавлено: {result['added']} | "
                           f"дубликатов: {result['duplicates_rejected']} | "
                           f"чёрный список: {result['blacklisted']}")
        self._show_message(f"📈 Источники: EXE {result['exe_scanned']} просмотрено, "
                           f"Пуск {result['start_menu']}, реестр {result['registry']}")
        return result

    def quick_scan_command(self):
        self._show_message("🚀 Быстрое сканирование: меню Пуск и реестр")
        added = 0
        for name, path in (self.app_scanner.scan_start_menu()
                           + self.app_scanner.get_installed_programs_from_registry()):
            if self.database.add_application(name, path):
                added += 1
        self._show_message(f"✅ Добавлено {added} программ")
        self.show_apps_command()

    def cleanup_database(self):
        cleaned = self.database.cleanup_duplicates()
        self._show_message(f"🧹 Удалено дубликатов: {cleaned}")
        self.show_stats()

    def show_stats(self):
        stats = self.database.get_database_stats()
        self._show_message("📊 Статистика базы:")
        self._show_message(f"   • Активных: {stats['active']}")
        self._show_message(f"   • Переименовано: {stats['renamed']}")
        self._show_message(f"   • Скрыто/удалено: {stats['inactive']}")
        self._show_message(f"   • В чёрном списке: {stats['blacklisted']}")

    def show_apps_command(self):
        apps = self.database.get_all_applications()
        self._show_message(f"📋 В базе {len(apps)} программ. Первые 10:")
        for app in apps[:10]:
            self._show_message(f"   • {app[2] or app[1]}")

    def show_popular_apps(self):
        self._show_message("⭐ Доступно без сканирования: калькулятор, блокнот, "
                           "проводник, paint, диспетчер задач, параметры Windows")

    def clear_database_completely_command(self):
        result = self.database.clear_database_completely()
        self._show_message(("✅ " if result["success"] else "❌ ") + result["message"])
        return result

    def clear_active_apps_command(self):
        result = self.database.clear_only_active_applications()
        self._show_message(("✅ " if result["success"] else "❌ ") + result["message"])
        return result

    def clear_blacklist_command(self):
        result = self.database.clear_only_blacklist()
        self._show_message(("✅ " if result["success"] else "❌ ") + result["message"])
        return result

    def show_help(self):
        for line in (
                "📖 СПРАВКА. Все команды начинаются со слова «Лора».",
                "",
                "Программы:",
                "   • Лора, открой калькулятор",
                "   • Лора, запусти хром",
                "   • Лора, закрой блокнот",
                "Медиа и звук:",
                "   • Лора, громче / тише",
                "   • Лора, выключить звук / включить звук",
                "   • Лора, пауза / продолжить",
                "   • Лора, следующая песня / предыдущая песня",
                "База:",
                "   • Лора, сканируй программы",
                "   • Лора, быстрое сканирование",
                "   • Лора, покажи программы / статистика",
                "Окно:",
                "   • Лора, свернись / развернись",
                "   • Лора, статус автозапуска",
                "   • Лора, пока",
        ):
            self._show_message(line)

    # ---------- вывод ----------
    def _show_message(self, message):
        logger.info(message)
        if self.gui_callback:
            self.gui_callback(message)

    def _show_error(self, error_msg):
        logger.error(error_msg)
        if self.gui_callback:
            self.gui_callback(error_msg)

    def _emit_status(self, status):
        if self.status_callback:
            self.status_callback(status)