import os
import csv
import sqlite3
import threading
from contextlib import contextmanager

from config import DB_PATH
from logger import logger
from utils import AdvancedTranslationSystem, normalize_name


class AppDatabase:
    """База приложений + чёрный список. WAL, кэш для быстрого сканирования."""

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self.translation_system = AdvancedTranslationSystem()
        self._lock = threading.RLock()
        self._index_cache = None
        self.init_database()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_database(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute('''
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_name TEXT NOT NULL,
                    custom_name TEXT,
                    exe_path TEXT UNIQUE NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS blacklist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exe_path TEXT UNIQUE NOT NULL,
                    original_name TEXT NOT NULL,
                    reason TEXT,
                    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cur.execute("CREATE INDEX IF NOT EXISTS idx_active ON applications(is_active)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_path ON applications(exe_path)")

    # ---------- кэш для дедупликации ----------
    def _invalidate_cache(self):
        with self._lock:
            self._index_cache = None

    def _get_index(self):
        with self._lock:
            if self._index_cache is None:
                with self._conn() as conn:
                    rows = conn.execute(
                        "SELECT original_name, exe_path FROM applications WHERE is_active=1"
                    ).fetchall()
                by_path, by_name = {}, {}
                for name, path in rows:
                    key = os.path.normcase(os.path.normpath(path))
                    by_path[key] = name
                    by_name.setdefault(normalize_name(name), []).append(key)
                self._index_cache = (by_path, by_name)
            return self._index_cache

    @staticmethod
    def normalize_app_name(name):
        return normalize_name(name)

    def is_duplicate(self, original_name, exe_path):
        """
        Дубликат = тот же путь, либо то же имя в той же папке.
        Раньше сравнивались все со всеми по похожести — это отсекало
        разные программы и тормозило сканирование.
        """
        by_path, by_name = self._get_index()
        key = os.path.normcase(os.path.normpath(exe_path))
        if key in by_path:
            return True
        folder = os.path.dirname(key)
        for existing_key in by_name.get(normalize_name(original_name), []):
            if os.path.dirname(existing_key) == folder:
                return True
        return False

    # ---------- чёрный список ----------
    def add_to_blacklist(self, exe_path, original_name, reason="Удалено пользователем"):
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO blacklist (exe_path, original_name, reason) "
                    "VALUES (?, ?, ?)", (exe_path, original_name, reason))
            return True
        except sqlite3.Error as e:
            logger.error(f"add_to_blacklist: {e}")
            return False

    def remove_from_blacklist(self, exe_path):
        try:
            with self._conn() as conn:
                cur = conn.execute("DELETE FROM blacklist WHERE exe_path = ?", (exe_path,))
            return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"remove_from_blacklist: {e}")
            return False

    def is_blacklisted(self, exe_path):
        with self._conn() as conn:
            return conn.execute(
                "SELECT 1 FROM blacklist WHERE exe_path = ?", (exe_path,)
            ).fetchone() is not None

    def get_blacklist(self):
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM blacklist ORDER BY added_date DESC"
            ).fetchall()

    # ---------- приложения ----------
    def add_application(self, original_name, exe_path, custom_name=None):
        """Добавление/обновление. Пользовательское имя НЕ затирается."""
        if self.is_blacklisted(exe_path):
            return False
        if self.is_duplicate(original_name, exe_path):
            return False
        try:
            with self._conn() as conn:
                conn.execute('''
                    INSERT INTO applications (original_name, custom_name, exe_path, is_active)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(exe_path) DO UPDATE SET
                        original_name = excluded.original_name,
                        custom_name = COALESCE(applications.custom_name, excluded.custom_name),
                        is_active = 1
                ''', (original_name, custom_name, exe_path))
            self._invalidate_cache()
            return True
        except sqlite3.Error as e:
            logger.error(f"add_application: {e}")
            return False

    def cleanup_duplicates(self):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, original_name, exe_path FROM applications "
                "WHERE is_active=1 ORDER BY last_used DESC"
            ).fetchall()
            seen, duplicates = set(), []
            for app_id, name, path in rows:
                key = (normalize_name(name),
                       os.path.normcase(os.path.dirname(os.path.normpath(path))))
                if key in seen:
                    duplicates.append(app_id)
                else:
                    seen.add(key)
            if duplicates:
                marks = ",".join("?" * len(duplicates))
                conn.execute(
                    f"UPDATE applications SET is_active=0 WHERE id IN ({marks})", duplicates)
        self._invalidate_cache()
        return len(duplicates)

    def get_all_applications(self):
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM applications WHERE is_active=1 "
                "ORDER BY COALESCE(custom_name, original_name)"
            ).fetchall()

    def get_application_by_name(self, name):
        """Точное совпадение -> нечёткий поиск. Возвращает строку БД или None."""
        if not name or not name.strip():
            return None
        target = name.strip().lower()
        rows = self.get_all_applications()
        for row in rows:
            for candidate in (row[2], row[1], normalize_name(row[1])):
                if candidate and candidate.lower() == target:
                    return row
        row, score = self.translation_system.find_best_row(name, rows, threshold=0.62)
        if row:
            logger.info(f"Поиск '{name}' -> '{row[2] or row[1]}' ({int(score*100)}%)")
        return row

    def search_applications(self, term, limit=5, threshold=0.35):
        rows = self.get_all_applications()
        scored = []
        for row in rows:
            name = row[2] or row[1]
            score = self.translation_system.find_best_match(term, name)
            if score >= threshold:
                scored.append((row, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def update_custom_name(self, exe_path, custom_name):
        with self._conn() as conn:
            conn.execute(
                "UPDATE applications SET custom_name=? WHERE exe_path=?",
                (custom_name, exe_path))
        self._invalidate_cache()

    def update_last_used(self, exe_path):
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE applications SET last_used=CURRENT_TIMESTAMP WHERE exe_path=?",
                    (exe_path,))
        except sqlite3.Error:
            pass

    def delete_application(self, exe_path):
        with self._conn() as conn:
            conn.execute("UPDATE applications SET is_active=0 WHERE exe_path=?", (exe_path,))
        self._invalidate_cache()

    def get_database_stats(self):
        with self._conn() as conn:
            active = conn.execute("SELECT COUNT(*) FROM applications WHERE is_active=1").fetchone()[0]
            inactive = conn.execute("SELECT COUNT(*) FROM applications WHERE is_active=0").fetchone()[0]
            unique = conn.execute(
                "SELECT COUNT(DISTINCT original_name) FROM applications WHERE is_active=1"
            ).fetchone()[0]
            renamed = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE is_active=1 AND custom_name IS NOT NULL"
            ).fetchone()[0]
            blacklisted = conn.execute("SELECT COUNT(*) FROM blacklist").fetchone()[0]
        return {"active": active, "inactive": inactive, "unique_names": unique,
                "renamed": renamed, "blacklisted": blacklisted}

    # ---------- очистка ----------
    @staticmethod
    def _reset_sequence(conn, table):
        try:
            conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
        except sqlite3.Error:
            pass  # таблицы sqlite_sequence может не быть

    def clear_database_completely(self):
        try:
            with self._conn() as conn:
                active = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
                black = conn.execute("SELECT COUNT(*) FROM blacklist").fetchone()[0]
                total = active + black
                conn.execute("DELETE FROM applications")
                conn.execute("DELETE FROM blacklist")
                self._reset_sequence(conn, "applications")
                self._reset_sequence(conn, "blacklist")
            self._invalidate_cache()
            return {"success": True, "cleared": total,
                    "message": f"Удалено {total} записей"}
        except sqlite3.Error as e:
            logger.error(f"clear_database_completely: {e}")
            return {"success": False, "cleared": 0, "message": f"Ошибка очистки: {e}"}

    def clear_only_active_applications(self):
        try:
            with self._conn() as conn:
                count = conn.execute("SELECT COUNT(*) FROM applications WHERE is_active=1").fetchone()[0]
                conn.execute("DELETE FROM applications WHERE is_active=1")
                self._reset_sequence(conn, "applications")
            self._invalidate_cache()
            return {"success": True, "cleared": count,
                    "message": f"Удалено {count} активных приложений"}
        except sqlite3.Error as e:
            return {"success": False, "cleared": 0, "message": f"Ошибка: {e}"}

    def clear_only_blacklist(self):
        try:
            with self._conn() as conn:
                count = conn.execute("SELECT COUNT(*) FROM blacklist").fetchone()[0]
                conn.execute("DELETE FROM blacklist")
                self._reset_sequence(conn, "blacklist")
            return {"success": True, "cleared": count,
                    "message": f"Удалено {count} записей чёрного списка"}
        except sqlite3.Error as e:
            return {"success": False, "cleared": 0, "message": f"Ошибка: {e}"}

    def export_to_csv(self, path):
        rows = self.get_all_applications()
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["Оригинальное имя", "Своё имя", "Путь", "Последний запуск"])
            for row in rows:
                writer.writerow([row[1], row[2] or "", row[3], row[5]])
        return len(rows)