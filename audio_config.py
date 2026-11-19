import os
import json
import hashlib
import platform
import threading
from pathlib import Path
from datetime import datetime, timedelta

from config import Config
from logger import logger


class AudioProfileManager:
    """Профили аудиоустройств: рабочие параметры + история успешных запусков"""

    def __init__(self):
        self._lock = threading.RLock()
        self.config_path = self._get_config_path()
        self.profiles = self._load_profiles()
        self.current_device_id = None
        self.auto_cleanup()

    def _get_config_path(self):
        config_dir = Path(Config.get_app_data_path()) / "AudioProfiles"
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir / "profiles.json"

    def generate_device_id(self, device_info):
        id_string = (f"{device_info.get('name','')}_"
                     f"{device_info.get('hostapi',0)}_"
                     f"{device_info.get('default_samplerate',0)}")
        api = device_info.get("api")
        if api:
            id_string += f"_{api}"
        return hashlib.md5(id_string.encode("utf-8")).hexdigest()[:16]

    def save_device_profile(self, device_info, working_params, success=True):
        with self._lock:
            device_id = self.generate_device_id(device_info)
            old = self.profiles.get(device_id, {})
            self.profiles[device_id] = {
                "device_id": device_id,
                "device_name": device_info.get("name", ""),
                "hostapi": device_info.get("hostapi", 0),
                "api": device_info.get("api", ""),
                "timestamp": old.get("timestamp", datetime.now().isoformat()),
                "last_used": datetime.now().isoformat(),
                "success_count": old.get("success_count", 0) + (1 if success else 0),
                "failure_count": old.get("failure_count", 0) + (0 if success else 1),
                "working_params": working_params,
                "system_info": self._get_system_fingerprint(),
            }
            self.current_device_id = device_id
            self._save_profiles()
            return device_id

    def get_device_profile(self, device_info):
        return self.profiles.get(self.generate_device_id(device_info))

    def _is_profile_valid(self, profile):
        try:
            last_used = datetime.fromisoformat(profile["last_used"])
            if datetime.now() - last_used > timedelta(days=90):
                return False
            if not isinstance(profile.get("working_params"), dict):
                return False
            if "sample_rate" not in profile["working_params"]:
                return False
            saved = profile.get("system_info", {})
            if saved.get("os_version") != platform.version():
                return False
            return True
        except Exception:
            return False

    def _get_system_fingerprint(self):
        return {
            "os_version": platform.version(),
            "os_release": platform.release(),
            "architecture": platform.architecture()[0],
            "processor": platform.processor(),
        }

    def get_best_params_for_device(self, device_info, supported_rates):
        profile = self.get_device_profile(device_info)
        if profile and self._is_profile_valid(profile):
            return dict(profile["working_params"])
        return self._find_best_params(device_info, supported_rates)

    def _find_best_params(self, device_info, supported_rates):
        preferred = [16000, 48000, 44100, 32000, 22050, 11025, 8000]
        rate = next((r for r in preferred if r in supported_rates), None)
        if rate is None:
            default_rate = int(device_info.get("default_samplerate", 44100))
            rate = default_rate if default_rate in supported_rates else (
                max(supported_rates) if supported_rates else 44100)
        return {"sample_rate": rate, "channels": 1, "dtype": "int16",
                "latency": "low", "blocksize": 1024}

    def _load_profiles(self):
        try:
            if self.config_path.exists():
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"Не удалось загрузить аудиопрофили: {e}")
        return {}

    def _save_profiles(self):
        try:
            tmp = self.config_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.profiles, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.config_path)
        except Exception as e:
            logger.warning(f"Не удалось сохранить аудиопрофили: {e}")

    def cleanup_old_profiles(self, max_age_days=90):
        cutoff = datetime.now() - timedelta(days=max_age_days)
        removed = 0
        with self._lock:
            for device_id, profile in list(self.profiles.items()):
                try:
                    if datetime.fromisoformat(profile["last_used"]) < cutoff:
                        del self.profiles[device_id]
                        removed += 1
                except (KeyError, ValueError, TypeError):
                    del self.profiles[device_id]
                    removed += 1
            if removed:
                self._save_profiles()
        return removed

    def auto_cleanup(self):
        removed = self.cleanup_old_profiles()
        with self._lock:
            before = len(self.profiles)
            self.profiles = {k: v for k, v in self.profiles.items() if self._is_profile_valid(v)}
            if before != len(self.profiles):
                self._save_profiles()
                removed += before - len(self.profiles)
        if removed:
            logger.info(f"Аудиопрофили: удалено {removed} устаревших записей")

    def get_statistics(self):
        return {
            "total": len(self.profiles),
            "successful": sum(1 for p in self.profiles.values() if p.get("success_count", 0) > 0),
        }