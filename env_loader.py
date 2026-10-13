"""Загрузка настроек из файла .env (лежит рядом с этим файлом).

Без сторонних библиотек. Уже заданные переменные окружения НЕ перезаписываются
(то есть то, что передано через systemd / docker / export, главнее файла .env).
"""
import os
from pathlib import Path

_loaded = False


def _parse_line(line):
    line = line.strip()
    if not line or line.startswith('#'):
        return None
    if line.startswith('export '):
        line = line[7:].lstrip()
    if '=' not in line:
        return None
    key, value = line.split('=', 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None
    if value[:1] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        value = value[1:end] if end != -1 else value[1:]
    else:
        # комментарий после значения: KEY=value  # комментарий
        for i, ch in enumerate(value):
            if ch == '#' and (i == 0 or value[i - 1].isspace()):
                value = value[:i].rstrip()
                break
    return key, value


def load_env(path=None):
    """Читает .env один раз за запуск. Возвращает True, если файл найден."""
    global _loaded
    if _loaded:
        return True
    env_path = Path(path) if path else Path(__file__).resolve().parent / '.env'
    if not env_path.is_file():
        _loaded = True
        return False
    with open(env_path, 'r', encoding='utf-8-sig') as f:
        for raw in f:
            parsed = _parse_line(raw)
            if parsed:
                key, value = parsed
                os.environ.setdefault(key, value)
    _loaded = True
    return True
