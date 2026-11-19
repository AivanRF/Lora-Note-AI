import re
from difflib import SequenceMatcher

try:
       import phonetics  # необязательная зависимость
except Exception:
       phonetics = None


def _fallback_soundex(word):
       """Простой Soundex, если пакет phonetics не установлен"""
       word = re.sub(r"[^a-z]", "", word.lower())
       if not word:
              return ""
       codes = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"),
                **dict.fromkeys("dt", "3"), "l": "4",
                **dict.fromkeys("mn", "5"), "r": "6"}
       result = word[0].upper()
       prev = codes.get(word[0], "")
       for ch in word[1:]:
              code = codes.get(ch, "")
              if code and code != prev:
                     result += code
              if ch not in "hw":
                     prev = code
              if len(result) == 4:
                     break
       return (result + "000")[:4]


def soundex(word):
       if phonetics is not None:
              try:
                     return phonetics.soundex(word)
              except Exception:
                     pass
       return _fallback_soundex(word)


def normalize_name(name):
       """Нормализация имени приложения для сравнения"""
       if not name:
              return ""
       name = name.lower().strip()
       name = re.sub(r"\.(exe|lnk|msi|appref-ms)$", "", name)
       name = re.sub(r"[\(\[].*?[\)\]]", " ", name)
       name = re.sub(r"\b(v|version|версия)\s*[\d\.]+\b", " ", name)
       name = re.sub(r"[\d\.]+$", "", name)
       name = re.sub(r"[^\w\s]", " ", name)
       name = re.sub(r"\s+", " ", name).strip()
       return name


def token_overlap(a, b):
       sa, sb = set(a.split()), set(b.split())
       if not sa or not sb:
              return 0.0
       return len(sa & sb) / len(sa | sb)


class AdvancedTranslationSystem:
       """Транслитерация + словарь русских названий английских программ"""

       def __init__(self):
              self.translit_dict = {
                     'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
                     'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
                     'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
                     'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
                     'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
              }
              self.translation_dict = {
                     'хром': ['chrome', 'google chrome'],
                     'гугл': ['google', 'chrome'],
                     'яндекс': ['yandex', 'yandex browser'],
                     'опера': ['opera'],
                     'файрфокс': ['firefox', 'mozilla firefox'],
                     'мозилла': ['firefox', 'mozilla firefox'],
                     'браузер': ['browser', 'chrome', 'edge'],
                     'телеграм': ['telegram'],
                     'телега': ['telegram'],
                     'ватсап': ['whatsapp'],
                     'дискорд': ['discord'],
                     'ворд': ['word', 'winword'],
                     'эксель': ['excel'],
                     'пауэр поинт': ['powerpoint', 'powerpnt'],
                     'аксесс': ['access', 'msaccess'],
                     'аутлук': ['outlook'],
                     'калькулятор': ['calculator', 'calc'],
                     'блокнот': ['notepad'],
                     'проводник': ['explorer'],
                     'панель управления': ['control panel', 'control'],
                     'диспетчер задач': ['task manager', 'taskmgr'],
                     'командная строка': ['cmd', 'command prompt', 'terminal'],
                     'терминал': ['terminal', 'cmd', 'windowsterminal'],
                     'редактор реестра': ['regedit'],
                     'пэйнт': ['paint', 'mspaint'],
                     'паинт': ['paint', 'mspaint'],
                     'вордпад': ['wordpad', 'write'],
                     'стим': ['steam'],
                     'эпик геймс': ['epic games', 'epicgameslauncher'],
                     'спотифай': ['spotify'],
                     'плеер': ['player', 'vlc', 'wmplayer'],
                     'обс': ['obs', 'obs64', 'obs studio'],
                     'вс код': ['code', 'vscode', 'visual studio code'],
                     'визуал студио': ['devenv', 'visual studio'],
                     'пайтон': ['python', 'pythonw'],
                     'питон': ['python', 'pythonw'],
                     'фотошоп': ['photoshop'],
                     'премьер': ['premiere', 'adobe premiere pro'],
                     'винрар': ['winrar'],
                     'севен зип': ['7zfm', '7-zip'],
                     'тимс': ['teams', 'msteams'],
                     'зум': ['zoom'],
                     'скайп': ['skype'],
                     'vlc': ['vlc'],
              }
              self.numeric_programs = {
                     '7-zip': ['7zip', '7 зип', 'семь зип', '7зип'],
                     'winrar': ['винрар', 'win rar'],
                     'visual studio code': ['vscode', 'vs code', 'вижуал студио код'],
                     'office 365': ['офис 365', 'офис365'],
                     'python 3': ['пайтон 3', 'питон 3'],
              }
              self.number_words = {
                     'ноль': '0', 'один': '1', 'два': '2', 'три': '3', 'четыре': '4',
                     'пять': '5', 'шесть': '6', 'семь': '7', 'восемь': '8', 'девять': '9',
                     'десять': '10', 'одиннадцать': '11', 'двенадцать': '12',
              }

       def transliterate_with_numbers(self, text):
              return "".join(self.translit_dict.get(ch, ch) for ch in text.lower())

       def _normalize_numbers(self, text):
              for word, digit in self.number_words.items():
                     text = re.sub(rf"\b{word}\b", digit, text)
              return text

       def transliterate_safe(self, text):
              return self.transliterate_with_numbers(self._normalize_numbers(text))

       def get_all_possible_names(self, russian_name):
              low = russian_name.lower().strip()
              variants = {low, normalize_name(low)}
              for ru, en_list in self.translation_dict.items():
                     if ru == low or ru in low:
                            variants.update(en_list)
              for en, ru_list in self.numeric_programs.items():
                     if any(v in low for v in ru_list):
                            variants.add(en)
              variants.add(self.transliterate_safe(low))
              return {normalize_name(v) for v in variants if v}

       def translate_app_name(self, russian_name):
              low = russian_name.lower().strip()
              for en, ru_list in self.numeric_programs.items():
                     if any(v in low for v in ru_list):
                            return en
              if low in self.translation_dict:
                     return self.translation_dict[low][0]
              for ru, en_list in self.translation_dict.items():
                     if ru in low:
                            return en_list[0]
              return self.transliterate_safe(low)

       def get_phonetic_similarity(self, w1, w2):
              a, b = soundex(w1), soundex(w2)
              return 1.0 if a and a == b else 0.0

       def find_best_match(self, spoken_name, app_name):
              """Оценка схожести 0..1"""
              s = normalize_name(spoken_name)
              a = normalize_name(app_name)
              if not s or not a:
                     return 0.0
              if s == a:
                     return 1.0

              best = 0.0
              for variant in self.get_all_possible_names(spoken_name):
                     if not variant:
                            continue
                     if variant == a:
                            return 0.98
                     if len(variant) >= 3 and (variant in a or a in variant):
                            best = max(best, 0.90)
                            continue
                     seq = SequenceMatcher(None, variant, a).ratio()
                     tok = token_overlap(variant, a)
                     phon = self.get_phonetic_similarity(variant, a)
                     best = max(best, seq * 0.55 + tok * 0.30 + phon * 0.15)
              return best

       def find_similar_app_name(self, spoken_name, available_apps, threshold=0.62):
              row, _ = self.find_best_row(spoken_name, available_apps, threshold)
              if row:
                     return row[2] or row[1]
              return None

       def find_best_row(self, spoken_name, rows, threshold=0.62):
              best_row, best_score = None, 0.0
              for row in rows:
                     original, custom = row[1], row[2]
                     for name in (custom, original):
                            if not name:
                                   continue
                            score = self.find_best_match(spoken_name, name)
                            if score > best_score:
                                   best_row, best_score = row, score
              if best_score >= threshold:
                     return best_row, best_score
              return None, best_score


class SecureVoiceCommandExtractor:
       """Извлечение команды с обязательным словом-активатором «Лора»"""

       # Vosk регулярно слышит косвенные формы — держим их в гейте
       KEYWORD_PATTERN = re.compile(
              r"\b(лора|лору|лоры|лоре|лорой|лорою|лара|лару|лары|ларе|ларой)\b"
       )

       # ВАЖНО: это множества, а не кортежи — иначе | не работает
       OPEN_VERBS = frozenset(("открой", "открыть", "запусти", "запустить",
                               "включи", "включить", "найди", "найти", "открывай"))
       CLOSE_VERBS = frozenset(("закрой", "закрыть", "выключи", "выключить",
                                "останови", "остановить", "заверши", "завершить"))
       SHOW_VERBS = frozenset(("покажи", "показать", "отобразить"))
       ALL_VERBS = OPEN_VERBS | CLOSE_VERBS | SHOW_VERBS

       FILLERS = frozenset(("пожалуйста", "мне", "можно", "бы", "же", "сейчас",
                            "ну", "вот", "это", "так", "а", "ка", "пожалуста",
                            "моя", "мой"))

       UNSECURED = ("завершить выполнение", "заверши выполнение")

       # Vosk рвёт слова на части: «с вернись» вместо «свернись»
       GLUE_MAP = {
              "с вернись": "свернись",
              "с вер нись": "свернись",
              "раз вернись": "развернись",
              "раз вер нись": "развернись",
              "с кройся": "скройся",
              "с канируй": "сканируй",
              "с кан ируй": "сканируй",
              "за пусти": "запусти",
              "от крой": "открой",
              "вы ключи": "выключи",
              "в ключи": "включи",
              "по ка": "пока",
              "про граммы": "программы",
              "с татус": "статус",
              "с татистика": "статистика",
       }
       GLUE_PREFIXES = frozenset(("с", "раз", "по", "за", "от", "вы", "в", "про", "при"))
       GLUE_TARGETS = frozenset(("свернись", "сверни", "развернись", "разверни",
                                 "скройся", "сканируй", "статус", "статистика",
                                 "программы", "пока", "запусти", "открой",
                                 "выключи", "включи", "продолжить", "справка"))

       def contains_required_keyword(self, text):
              return bool(self.KEYWORD_PATTERN.search(text.lower()))

       def is_unsecured_command(self, text):
              low = text.lower()
              return any(cmd in low for cmd in self.UNSECURED)

       def _remove_keyword(self, text):
              return self.KEYWORD_PATTERN.sub(" ", text)

       def _remove_filler_words(self, text):
              return " ".join(w for w in text.split() if w not in self.FILLERS)

       def _glue_broken_words(self, text):
              for broken, fixed in self.GLUE_MAP.items():
                     text = text.replace(broken, fixed)
              words = text.split()
              result, i = [], 0
              while i < len(words):
                     if (i + 1 < len(words)
                             and words[i] in self.GLUE_PREFIXES
                             and words[i] + words[i + 1] in self.GLUE_TARGETS):
                            result.append(words[i] + words[i + 1])
                            i += 2
                     else:
                            result.append(words[i])
                            i += 1
              return " ".join(result)

       def extract_command(self, text):
              """Возвращает (команда, уверенность)"""
              low = text.lower().strip()
              if not self.contains_required_keyword(low) and not self.is_unsecured_command(low):
                     return None, 0.0

              clean = self._remove_keyword(low)
              clean = re.sub(r"[^\w\s\-\.]", " ", clean)
              clean = self._glue_broken_words(clean)
              clean = self._remove_filler_words(clean)
              clean = " ".join(clean.split()).strip()
              if not clean:
                     return None, 0.0

              words = clean.split()
              verb, rest = words[0], " ".join(words[1:]).strip()

              if verb in self.OPEN_VERBS and rest:
                     return f"открой {rest}", 0.95
              if verb in self.CLOSE_VERBS and rest:
                     return f"закрой {rest}", 0.92
              if verb in self.SHOW_VERBS and rest:
                     return f"покажи {rest}", 0.90
              if verb in self.ALL_VERBS:
                     return None, 0.0
              return clean, 0.80