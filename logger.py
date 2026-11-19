import os
import sys
import logging
import traceback
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timedelta

from config import Config

# В windowed-сборке sys.stderr может быть None — logging не должен падать
logging.raiseExceptions = False


class AppLogger:
    """Логирование в файл с ротацией по дням"""

    def __init__(self):
        self.logs_dir = os.path.join(Config.get_app_data_path(), "logs")
        os.makedirs(self.logs_dir, exist_ok=True)
        self.log_file = os.path.join(self.logs_dir, "Lora_AI.log")
        self.logger = logging.getLogger("LoraAI")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self._setup_handlers()
        self.cleanup_old_logs()

    def _setup_handlers(self):
        if self.logger.handlers:  # защита от дублей при повторном импорте
            return
        fmt = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S"
        )
        try:
            fh = TimedRotatingFileHandler(
                self.log_file, when="midnight", interval=1,
                backupCount=7, encoding="utf-8"
            )
            fh.setFormatter(fmt)
            self.logger.addHandler(fh)
        except Exception as e:
            print(f"Не удалось создать файловый лог: {e}")

        if sys.stderr is not None:
            ch = logging.StreamHandler()
            ch.setFormatter(fmt)
            self.logger.addHandler(ch)

    def cleanup_old_logs(self):
        try:
            cutoff = datetime.now() - timedelta(days=7)
            for name in os.listdir(self.logs_dir):
                path = os.path.join(self.logs_dir, name)
                if not os.path.isfile(path) or path == self.log_file:
                    continue
                if ".log" not in name:
                    continue
                if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        except Exception:
            pass

    def info(self, message):
        self.logger.info(message)

    def warning(self, message):
        self.logger.warning(message)

    def error(self, message):
        self.logger.error(message)

    def exception(self, message):
        self.logger.error(f"{message}\n{traceback.format_exc()}")

    def get_log_file_path(self):
        return self.log_file

    def get_recent_logs(self, lines=50):
        try:
            if os.path.exists(self.log_file):
                with open(self.log_file, "r", encoding="utf-8", errors="replace") as f:
                    return "".join(f.readlines()[-lines:])
        except Exception as e:
            return f"Ошибка чтения логов: {e}"
        return "Логи не найдены"


logger = AppLogger()