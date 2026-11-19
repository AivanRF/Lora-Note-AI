import os
import json
import threading

from config import Config


class UserPreferences:
    """Пользовательские настройки (потокобезопасно, атомарная запись)"""

    DEFAULTS = {
        "autostart_enabled": False,        # честный дефолт: спрашиваем при первом запуске
        "user_modified_autostart": False,
        "first_run": True,
        "autostart_attempts": 0,
        "last_autostart_check": None,
        "auto_listen": True,
        "selected_device_index": None,
        "selected_sample_rate": None,
        "confirm_destructive": True,
    }

    def __init__(self):
        self.prefs_file = os.path.join(Config.get_app_data_path(), "user_preferences.json")
        self._lock = threading.RLock()
        self.preferences = self._load_preferences()

    def _load_preferences(self):
        prefs = dict(self.DEFAULTS)
        try:
            if os.path.exists(self.prefs_file):
                with open(self.prefs_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    prefs.update(loaded)
        except Exception:
            # битый файл не должен ломать запуск
            try:
                os.replace(self.prefs_file, self.prefs_file + ".broken")
            except OSError:
                pass
        return prefs

    def save_preferences(self):
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.prefs_file), exist_ok=True)
                tmp = self.prefs_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.preferences, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.prefs_file)
                return True
            except Exception:
                return False

    def get(self, key, default=None):
        with self._lock:
            return self.preferences.get(key, default)

    def set(self, key, value):
        with self._lock:
            self.preferences[key] = value
        return self.save_preferences()

    # --- автозапуск ---
    def is_autostart_allowed(self):
        if self.get("user_modified_autostart", False):
            return False
        if self.get("autostart_attempts", 0) >= 3:
            return False
        return bool(self.get("autostart_enabled", False))

    def mark_user_modified_autostart(self, enabled):
        with self._lock:
            self.preferences["user_modified_autostart"] = True
            self.preferences["autostart_enabled"] = bool(enabled)
        self.save_preferences()

    def mark_autostart_attempt(self, success):
        with self._lock:
            if success:
                self.preferences["autostart_attempts"] = 0
            else:
                self.preferences["autostart_attempts"] = self.get("autostart_attempts", 0) + 1
            self.preferences["first_run"] = False
        self.save_preferences()

    def get_autostart_status(self):
        return bool(self.get("autostart_enabled", False))

    def set_autostart_status(self, enabled):
        return self.set("autostart_enabled", bool(enabled))