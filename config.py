import os
import sys

APP_VERSION = "Beta 0.9"
APP_BUILD_DATE = "19.11.2026"
APP_AUTHOR = "Карпов Иван (Айван)"


class Config:
       """Конфигурация с поддержкой портативного режима"""

       SAMPLE_RATE = 16000
       CHUNK_SIZE = 4000
       APP_NAME = "Lora AI"
       MODEL_DIR_NAME = "vosk-model-small-ru-0.22"

       @staticmethod
       def is_frozen():
              return getattr(sys, "frozen", False)

       @staticmethod
       def base_dir():
              """Папка, рядом с которой лежит программа"""
              if Config.is_frozen():
                     return os.path.dirname(sys.executable)
              return os.path.dirname(os.path.abspath(__file__))

       @staticmethod
       def resource_path(relative):
              """Путь к встроенному ресурсу (работает и в onefile, и в onedir)"""
              bundled = getattr(sys, "_MEIPASS", None)
              if bundled:
                     candidate = os.path.join(bundled, relative)
                     if os.path.exists(candidate):
                            return candidate
              return os.path.join(Config.base_dir(), relative)

       @staticmethod
       def is_portable_mode():
              return os.path.exists(os.path.join(Config.base_dir(), "portable.txt"))

       @staticmethod
       def get_app_data_path():
              if Config.is_portable_mode():
                     app_dir = os.path.join(Config.base_dir(), "Lora_AI_Data")
              else:
                     app_data = os.getenv("APPDATA") or os.path.expanduser("~")
                     app_dir = os.path.join(app_data, "Lora AI")
              os.makedirs(app_dir, exist_ok=True)
              return app_dir

       @staticmethod
       def get_db_path():
              return os.path.join(Config.get_app_data_path(), "apps.db")

       @staticmethod
       def get_model_path():
              candidates = [
                     os.path.join(Config.base_dir(), Config.MODEL_DIR_NAME),
                     os.path.join(Config.get_app_data_path(), Config.MODEL_DIR_NAME),
                     os.path.join(getattr(sys, "_MEIPASS", Config.base_dir()), Config.MODEL_DIR_NAME),
              ]
              for path in candidates:
                     if os.path.isdir(path):
                            return path
              return candidates[0]

       @staticmethod
       def ensure_directories():
              data_dir = Config.get_app_data_path()
              for subdir in ("logs", "backups", "cache", "reports"):
                     os.makedirs(os.path.join(data_dir, subdir), exist_ok=True)
              return data_dir


# Глобальные константы для обратной совместимости
SAMPLE_RATE = Config.SAMPLE_RATE
CHUNK_SIZE = Config.CHUNK_SIZE
MODEL_PATH = Config.get_model_path()
DB_PATH = Config.get_db_path()
APP_NAME = Config.APP_NAME