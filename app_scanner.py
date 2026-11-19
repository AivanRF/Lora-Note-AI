import os
import time
import string
import winreg

from logger import logger
from system_launcher import CREATE_NO_WINDOW  # noqa: F401  (константа для единообразия)

# Папки, которые не обходим вообще (по имени каталога, а не по подстроке пути)
SKIP_DIR_NAMES = {
    "windows", "$recycle.bin", "system volume information", "winsxs", "servicing",
    "installer", "node_modules", "__pycache__", ".git", ".svn", ".vs", "obj",
    "temp", "tmp", "cache", "cachet", "logs", "log", "crashpad", "crashreports",
    "locales", "swiftshader", "driverstore", "assembly", "gac_msil", "gac_32",
    "gac_64", "microsoft shared", "common files", "redist", "redistributable",
    "vcredist", "dotnet", "sdk", "sdks", "documentation", "help", "docs",
}

UNINSTALL_TOKENS = ("uninst", "unins", "uninstal", "deinstall", "remove",
                    "setup", "install", "update", "updater", "patch", "hotfix",
                    "vcredist", "redist", "crashpad_handler", "crashreport",
                    "elevation_service", "notification_helper")

MIN_FILE_SIZE = 100 * 1024   # было 500 КБ — отсекало половину полезных утилит


class AppScanner:
    """Сканер приложений: Program Files, меню Пуск, реестр. С прогрессом и отменой."""

    def __init__(self, database, max_files=5000, max_depth=8):
        self.database = database
        self.max_files = max_files
        self.max_depth = max_depth
        self.common_paths = self._get_scan_roots()

    def _get_scan_roots(self):
        roots = []
        if os.name == "nt":
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if not os.path.exists(drive):
                    continue
                for sub in ("Program Files", "Program Files (x86)"):
                    path = os.path.join(drive, sub)
                    if os.path.isdir(path):
                        roots.append(path)
        local = os.getenv("LOCALAPPDATA")
        if local:
            programs = os.path.join(local, "Programs")
            if os.path.isdir(programs):
                roots.append(programs)
        logger.info(f"Каталоги сканирования: {roots}")
        return roots

    # ---------- фильтры ----------
    @staticmethod
    def _should_skip_directory(dir_path):
        name = os.path.basename(dir_path).lower()
        if name in SKIP_DIR_NAMES:
            return True
        low = dir_path.lower()
        return "windows\\system32" in low or "windows\\syswow64" in low

    @staticmethod
    def is_uninstall_file(file_name, file_path=None):
        low = file_name.lower()
        return any(token in low for token in UNINSTALL_TOKENS)

    @staticmethod
    def get_file_size(file_path):
        try:
            return os.path.getsize(file_path)
        except OSError:
            return 0

    def is_valid_application(self, file_name, file_path):
        if self.is_uninstall_file(file_name):
            return False
        if self.get_file_size(file_path) < MIN_FILE_SIZE:
            return False
        return True

    # ---------- сканирование ----------
    def scan_system_for_exe(self, progress=None, cancel_event=None):
        found, scanned, filtered = [], 0, 0
        for root_path in self.common_paths:
            if cancel_event is not None and cancel_event.is_set():
                break
            if not os.path.isdir(root_path):
                continue
            base_depth = root_path.rstrip("\\").count(os.sep)
            for root, dirs, files in os.walk(root_path, onerror=lambda e: None):
                if cancel_event is not None and cancel_event.is_set():
                    break
                if root.count(os.sep) - base_depth >= self.max_depth:
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in dirs if not self._should_skip_directory(os.path.join(root, d))]
                for file in files:
                    if not file.lower().endswith(".exe"):
                        continue
                    scanned += 1
                    full_path = os.path.join(root, file)
                    if self.is_valid_application(file, full_path):
                        found.append((file, full_path))
                    else:
                        filtered += 1
                    if progress is not None and scanned % 200 == 0:
                        progress({"stage": "exe", "scanned": scanned,
                                  "found": len(found), "current": root})
                    if len(found) >= self.max_files:
                        break
                if len(found) >= self.max_files:
                    break
        logger.info(f"EXE: просмотрено {scanned}, подходящих {len(found)}, отфильтровано {filtered}")
        return found, {"scanned": scanned, "filtered": filtered}

    def scan_start_menu(self):
        paths = [
            os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
            r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
        ]
        apps = []
        for start_path in paths:
            if not os.path.isdir(start_path):
                continue
            for root, dirs, files in os.walk(start_path, onerror=lambda e: None):
                for file in files:
                    if file.lower().endswith((".lnk", ".url")):
                        name = os.path.splitext(file)[0]
                        if self.is_uninstall_file(file):
                            continue
                        apps.append((name, os.path.join(root, file)))
        logger.info(f"Меню Пуск: {len(apps)} ярлыков")
        return apps

    @staticmethod
    def _clean_icon_path(icon):
        if not icon:
            return None
        icon = os.path.expandvars(icon.strip().strip('"'))
        if "," in icon:                      # "C:\app\app.exe,0"
            icon = icon.rsplit(",", 1)[0].strip().strip('"')
        return icon if icon.lower().endswith(".exe") else None

    def get_installed_programs_from_registry(self):
        programs = []
        key_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, key_path in key_paths:
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    for i in range(count):
                        try:
                            sub_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, sub_name) as sub:
                                try:
                                    name = winreg.QueryValueEx(sub, "DisplayName")[0]
                                except FileNotFoundError:
                                    continue
                                try:
                                    system_component = winreg.QueryValueEx(sub, "SystemComponent")[0]
                                    if system_component == 1:
                                        continue
                                except FileNotFoundError:
                                    pass
                                icon = None
                                for value in ("DisplayIcon", "InstallLocation"):
                                    try:
                                        icon = self._clean_icon_path(winreg.QueryValueEx(sub, value)[0])
                                        if icon:
                                            break
                                    except FileNotFoundError:
                                        continue
                                if icon and os.path.exists(icon):
                                    programs.append((name, icon))
                        except OSError:
                            continue
            except FileNotFoundError:
                continue
            except OSError as e:
                logger.warning(f"Реестр {key_path}: {e}")
        logger.info(f"Реестр: {len(programs)} программ")
        return programs

    def populate_database(self, progress=None, cancel_event=None):
        started = time.time()

        def report(payload):
            if progress is not None:
                progress(payload)

        report({"stage": "start", "scanned": 0, "found": 0, "current": ""})
        exe_files, exe_stats = self.scan_system_for_exe(progress, cancel_event)
        report({"stage": "start_menu", "scanned": exe_stats["scanned"],
                "found": len(exe_files), "current": "Меню Пуск"})
        start_menu = self.scan_start_menu()
        report({"stage": "registry", "scanned": exe_stats["scanned"],
                "found": len(exe_files) + len(start_menu), "current": "Реестр"})
        registry = self.get_installed_programs_from_registry()

        all_apps = exe_files + start_menu + registry
        added = duplicates = blacklisted = 0
        for index, (name, path) in enumerate(all_apps):
            if cancel_event is not None and cancel_event.is_set():
                break
            if self.database.is_blacklisted(path):
                blacklisted += 1
                continue
            if self.database.add_application(name, path):
                added += 1
            else:
                duplicates += 1
            if index % 100 == 0:
                report({"stage": "database", "scanned": index,
                        "found": added, "current": name})

        cleaned = self.database.cleanup_duplicates()
        report({"stage": "done", "scanned": len(all_apps), "found": added, "current": ""})
        return {
            "added": added,
            "duplicates_rejected": duplicates,
            "duplicates_cleaned": cleaned,
            "blacklisted": blacklisted,
            "total_processed": len(all_apps),
            "exe_scanned": exe_stats["scanned"],
            "exe_filtered": exe_stats["filtered"],
            "start_menu": len(start_menu),
            "registry": len(registry),
            "elapsed": round(time.time() - started, 1),
            "cancelled": bool(cancel_event is not None and cancel_event.is_set()),
        }