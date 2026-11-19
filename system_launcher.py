import os
import subprocess
import sys

from logger import logger

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class SystemLauncher:
    """Запуск системных приложений Windows + универсальный запуск файлов"""

    # каноническое имя -> (цель, алиасы)
    SYSTEM_APPS = {
        "калькулятор": ("calc.exe", ("calculator", "calc")),
        "блокнот": ("notepad.exe", ("notepad",)),
        "проводник": ("explorer.exe", ("explorer", "файлы")),
        "paint": ("mspaint.exe", ("паинт", "пэйнт", "рисование")),
        "командная строка": ("cmd.exe", ("cmd", "консоль")),
        "панель управления": ("control.exe", ("control",)),
        "диспетчер задач": ("taskmgr.exe", ("task manager", "taskmgr")),
        "wordpad": ("write.exe", ("вордпад",)),
        "браузер edge": ("microsoft-edge:", ("edge", "эдж")),
        "редактор реестра": ("regedit.exe", ("regedit", "реестр")),
        "настройки звука": ("mmsys.cpl", ("панель звука", "устройства звука")),
        "настройки сети": ("ncpa.cpl", ("сетевые подключения",)),
        "свойства системы": ("sysdm.cpl", ("о системе",)),
        "параметры windows": ("ms-settings:", ("параметры", "настройки windows")),
        "ножницы": ("SnippingTool.exe", ("скриншот", "snipping tool")),
        "камера": ("microsoft.windows.camera:", ("вебкамера",)),
        "экранная лупа": ("magnify.exe", ("лупа",)),
        "таблица символов": ("charmap.exe", ()),
        "очистка диска": ("cleanmgr.exe", ()),
        "удаленный рабочий стол": ("mstsc.exe", ("rdp",)),
    }

    OPEN_PREFIXES = ("открой ", "запусти ", "включи ", "найди ", "покажи ")

    @staticmethod
    def _strip_verb(text):
        low = text.lower().strip()
        for prefix in SystemLauncher.OPEN_PREFIXES:
            if low.startswith(prefix):
                return low[len(prefix):].strip()
        return low

    @staticmethod
    def match(command_text):
        """
        Возвращает (имя, цель) только при уверенном совпадении.
        Специально НЕ матчим одиночные слова 'звук'/'сеть'/'система',
        иначе перехватываются команды громкости.
        """
        name = SystemLauncher._strip_verb(command_text)
        if not name:
            return None
        for ru_name, (target, aliases) in SystemLauncher.SYSTEM_APPS.items():
            candidates = (ru_name,) + tuple(aliases)
            for candidate in candidates:
                if name == candidate:
                    return ru_name, target
        # составные названия допускаем как вхождение
        for ru_name, (target, aliases) in SystemLauncher.SYSTEM_APPS.items():
            if len(ru_name.split()) >= 2 and ru_name in name:
                return ru_name, target
        return None

    @staticmethod
    def is_system_app(command_text):
        return SystemLauncher.match(command_text) is not None

    @staticmethod
    def launch_system_app(command_text):
        match = SystemLauncher.match(command_text)
        if not match:
            return False, None
        ru_name, target = match
        ok, err = SystemLauncher.launch_target(target)
        if ok:
            return True, f"🚀 Запускаю {ru_name}..."
        return False, f"❌ Ошибка запуска {ru_name}: {err}"

    @staticmethod
    def launch_target(target):
        """Запуск системной цели: exe, .cpl, URI-схема"""
        try:
            if target.endswith(":") or target.startswith("ms-"):
                os.startfile(target)
                return True, None
            if target.lower().endswith(".cpl"):
                subprocess.Popen(["control.exe", target],
                                 creationflags=CREATE_NO_WINDOW)
                return True, None
            subprocess.Popen([target], creationflags=CREATE_NO_WINDOW)
            return True, None
        except Exception as e:
            logger.error(f"launch_target({target}): {e}")
            return False, str(e)

    @staticmethod
    def launch_path(path):
        """Универсальный запуск: .exe, .lnk, .bat, документы"""
        if not path:
            return False, "Пустой путь"
        path = os.path.expandvars(path.strip().strip('"'))
        if not os.path.exists(path):
            return False, "Файл не найден"
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext in (".lnk", ".url", ".appref-ms"):
                os.startfile(path)          # ярлыки Popen не понимает
            elif ext == ".exe":
                subprocess.Popen([path], cwd=os.path.dirname(path) or None,
                                 creationflags=CREATE_NO_WINDOW)
            elif ext in (".bat", ".cmd"):
                subprocess.Popen(["cmd", "/c", path],
                                 creationflags=CREATE_NO_WINDOW)
            elif ext == ".msi":
                subprocess.Popen(["msiexec", "/i", path],
                                 creationflags=CREATE_NO_WINDOW)
            else:
                os.startfile(path)
            return True, None
        except Exception as e:
            logger.error(f"launch_path({path}): {e}")
            return False, str(e)

    @staticmethod
    def close_by_exe_path(path):
        """Мягкое закрытие процесса по имени исполняемого файла"""
        if not path:
            return False, "Неизвестный путь"
        image = os.path.basename(path)
        if not image.lower().endswith(".exe"):
            return False, "Это ярлык — закрытие не поддерживается"
        try:
            result = subprocess.run(
                ["taskkill", "/IM", image],
                capture_output=True, text=True,
                encoding="cp866", errors="replace",
                creationflags=CREATE_NO_WINDOW
            )
            if result.returncode == 0:
                return True, None
            return False, (result.stdout or result.stderr or "").strip()
        except Exception as e:
            return False, str(e)