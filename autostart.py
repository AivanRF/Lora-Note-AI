import os
import sys
import winreg
from pathlib import Path

from config import APP_NAME
from logger import logger
from user_preferences import UserPreferences

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class SmartAutoStartManager:
    """Автозапуск: одна запись, честная логика восстановления"""

    def __init__(self, app_name=APP_NAME):
        self.app_name = app_name
        self.preferences = UserPreferences()
        self.command = self._build_command()

    @staticmethod
    def _build_command():
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}" --hidden'
        script = os.path.abspath(sys.argv[0])
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        interpreter = pythonw if os.path.exists(pythonw) else sys.executable
        return f'"{interpreter}" "{script}" --hidden'

    def _get_startup_folder(self):
        appdata = os.getenv("APPDATA") or os.path.expanduser("~")
        folder = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _remove_entries(self):
        """Удаляет записи автозапуска, не трогая настройки пользователя"""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                try:
                    winreg.DeleteValue(key, self.app_name)
                except FileNotFoundError:
                    pass
        except OSError:
            pass
        folder = self._get_startup_folder()
        for ext in ("vbs", "bat", "cmd"):
            path = folder / f"{self.app_name}.{ext}"
            if path.exists():
                try:
                    path.unlink()
                    logger.info(f"Удалён файл автозапуска: {path.name}")
                except OSError:
                    pass

    LEGACY_NAMES = ("Lora AI", "VoiceControlLora", "70Karpov", "Lora")

    def get_registry_command(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_READ) as key:
                return winreg.QueryValueEx(key, self.app_name)[0]
        except OSError:
            return None

    def reconcile(self):
        """Приводим систему в соответствие с волей пользователя.
        Убирает записи, оставленные старыми версиями программы."""
        system_enabled = self.is_autostart_enabled()
        want_enabled = self.preferences.get_autostart_status()
        user_modified = self.preferences.get("user_modified_autostart", False)

        if system_enabled and not want_enabled:
            logger.info("Автозапуск в системе есть, но в настройках выключен — убираю")
            self._remove_entries()
            return "removed"

        if system_enabled and want_enabled:
            # могли остаться дубли от старой версии (реестр + vbs + bat)
            folder = self._get_startup_folder()
            has_registry = self.get_registry_command() is not None
            if has_registry:
                self._cleanup_other_methods("_try_registry_current_user")
            for legacy in self.LEGACY_NAMES:
                if legacy == self.app_name:
                    continue
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                        winreg.KEY_SET_VALUE) as key:
                        winreg.DeleteValue(key, legacy)
                        logger.info(f"Удалена старая запись автозапуска: {legacy}")
                except OSError:
                    pass
                for ext in ("vbs", "bat", "cmd"):
                    old = folder / f"{legacy}.{ext}"
                    if old.exists():
                        try:
                            old.unlink()
                        except OSError:
                            pass
            return "cleaned"

        if not system_enabled and want_enabled:
            return "needs_restore"
        return "ok"

    # ---------- включение ----------
    def enable_autostart(self):
        self.preferences.preferences["user_modified_autostart"] = False
        success = self._try_enable_safely()
        self.preferences.mark_autostart_attempt(success)
        self.preferences.set_autostart_status(bool(success))
        logger.info("Автозапуск включён" if success else "Не удалось включить автозапуск")
        return success

    def _try_enable_safely(self):
        # ВАЖНО: раньше выполнялись все методы -> программа стартовала трижды
        for method in (self._try_registry_current_user,
                       self._try_startup_folder_vbs,
                       self._try_startup_folder_bat):
            try:
                if method():
                    logger.info(f"Автозапуск настроен: {method.__name__}")
                    self._cleanup_other_methods(method.__name__)
                    return True
            except Exception as e:
                logger.warning(f"{method.__name__}: {e}")
        return False

    def _try_registry_current_user(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, self.app_name, 0, winreg.REG_SZ, self.command)
            return True
        except OSError:
            return False

    def _try_startup_folder_vbs(self):
        try:
            path = self._get_startup_folder() / f"{self.app_name}.vbs"
            vbs_command = self.command.replace('"', '""')
            content = (
                "' Lora AI autostart\r\n"
                'Set sh = CreateObject("WScript.Shell")\r\n'
                f'sh.Run "{vbs_command}", 0, False\r\n'
                "Set sh = Nothing\r\n"
            )
            path.write_text(content, encoding="utf-8")
            return True
        except OSError:
            return False

    def _try_startup_folder_bat(self):
        try:
            path = self._get_startup_folder() / f"{self.app_name}.bat"
            content = f'@echo off\r\nstart "" {self.command}\r\nexit\r\n'
            path.write_text(content, encoding="utf-8")
            return True
        except OSError:
            return False

    def _cleanup_other_methods(self, used):
        """Убираем записи, созданные старыми версиями, чтобы не было дублей"""
        if used != "_try_registry_current_user":
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, self.app_name)
            except OSError:
                pass
        folder = self._get_startup_folder()
        keep = {"_try_startup_folder_vbs": "vbs", "_try_startup_folder_bat": "bat"}.get(used)
        for ext in ("vbs", "bat", "cmd"):
            if ext == keep:
                continue
            file_path = folder / f"{self.app_name}.{ext}"
            if file_path.exists():
                try:
                    file_path.unlink()
                except OSError:
                    pass

    # ---------- отключение ----------
    def disable_autostart(self):
        errors = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                try:
                    winreg.DeleteValue(key, self.app_name)
                except FileNotFoundError:
                    pass  # записи не было — это не ошибка
        except OSError as e:
            errors.append(str(e))

        folder = self._get_startup_folder()
        for ext in ("vbs", "bat", "cmd"):
            file_path = folder / f"{self.app_name}.{ext}"
            if file_path.exists():
                try:
                    file_path.unlink()
                except OSError as e:
                    errors.append(str(e))

        success = not errors
        if success:
            self.preferences.mark_user_modified_autostart(False)
            logger.info("Автозапуск отключён пользователем")
        else:
            logger.error(f"Автозапуск отключён не полностью: {errors}")
        return success

    def toggle_autostart(self, enable):
        return self.enable_autostart() if enable else self.disable_autostart()

    # ---------- состояние ----------
    def is_autostart_enabled(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, self.app_name)
                return True
        except OSError:
            pass
        folder = self._get_startup_folder()
        return any((folder / f"{self.app_name}.{ext}").exists()
                   for ext in ("vbs", "bat", "cmd"))

    def should_restore_autostart(self):
        if self.preferences.get("user_modified_autostart", False):
            return False
        if not self.preferences.get_autostart_status():
            return False
        if self.preferences.get("autostart_attempts", 0) >= 3:
            return False
        return True

    def restore_autostart_if_needed(self):
        if self.is_autostart_enabled():
            return False
        if not self.should_restore_autostart():
            return False
        logger.info("Восстанавливаю автозапуск согласно настройкам")
        return self._try_enable_safely()

    def get_status(self):
        return {
            "system": self.is_autostart_enabled(),
            "prefs": self.preferences.get_autostart_status(),
            "user_modified": self.preferences.get("user_modified_autostart", False),
            "registry_command": self.get_registry_command(),
            "command": self.command,
            "attempts": self.preferences.get("autostart_attempts", 0),
        }