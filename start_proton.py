# -*- coding: utf-8 -*-
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / "venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
BAT_FILE = ROOT / "start_proton.bat"
AUTOSTART_NAME = "ProtonMessenger_autostart.bat"
AUTOSTART_FILE = (
    Path(os.environ["APPDATA"])
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs"
    / "Startup"
    / AUTOSTART_NAME
)
PROTON_PY = ROOT / "proton.py"


def pause():
    try:
        input("\nНажмите Enter для выхода...")
    except (EOFError, KeyboardInterrupt):
        pass


def find_system_python():
    for name in ("py", "python"):
        if shutil.which(name):
            return name
    return None


def autostart_enabled():
    return AUTOSTART_FILE.is_file()


def enable_autostart():
    bat_path = str(BAT_FILE.resolve())
    root_path = str(ROOT.resolve())
    content = (
        "@echo off\r\n"
        f'cd /d "{root_path}"\r\n'
        f'call "{bat_path}" /autostart\r\n'
    )
    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART_FILE.write_text(content, encoding="ascii")
    print("\nАвтозагрузка включена.")
    print("Мессенджер будет запускаться при входе в Windows.")


def disable_autostart():
    if AUTOSTART_FILE.is_file():
        AUTOSTART_FILE.unlink()
        print("\nАвтозагрузка отключена.")
    else:
        print("\nАвтозагрузка уже была выключена.")


def run_cmd(cmd, **kwargs):
    print()
    result = subprocess.run(cmd, cwd=ROOT, **kwargs)
    return result.returncode


def setup_venv():
    system_python = find_system_python()
    if not system_python:
        print("Ошибка: Python не найден. Установите Python и добавьте в PATH.")
        return False

    print("venv не найден. Создаю окружение...")
    code = run_cmd([system_python, "-m", "venv", str(VENV_DIR)])
    if code != 0:
        print("Ошибка создания venv.")
        return False

    if not (ROOT / "requirements.txt").is_file():
        print("Ошибка: файл requirements.txt не найден.")
        return False

    if not (ROOT / "offline_packages").is_dir():
        print("Ошибка: папка offline_packages не найдена.")
        return False

    print("Устанавливаю зависимости из offline_packages...")
    code = run_cmd(
        [
            str(VENV_PYTHON),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links=offline_packages",
            "-r",
            "requirements.txt",
        ]
    )
    if code != 0:
        print("Ошибка установки зависимостей.")
        return False

    print("\nОкружение готово.")
    return True


def run_messenger(autostart=False):
    print("\nЗапуск Proton Messenger...")

    if not VENV_PYTHON.is_file():
        if not setup_venv():
            return 1

    if not PROTON_PY.is_file():
        print("Ошибка: файл proton.py не найден.")
        return 1

    code = run_cmd([str(VENV_PYTHON), str(PROTON_PY)])

    if not autostart:
        print()
        if code != 0:
            print(f"Мессенджер завершился с кодом {code}.")
        else:
            print("Мессенджер остановлен.")

    return code


def show_menu():
    while True:
        os.system("cls")
        print("========================================")
        print("        Proton Messenger")
        print("========================================")
        print()
        print(" Автозагрузка:", "ВКЛЮЧЕНА" if autostart_enabled() else "ВЫКЛЮЧЕНА")
        print()
        print(" 1. Запустить мессенджер")
        print(" 2. Включить автозагрузку")
        print(" 3. Отключить автозагрузку")
        print(" 4. Выход")
        print()

        try:
            choice = input("Выберите действие (1-4): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice == "1":
            code = run_messenger(autostart=False)
            pause()
            if code != 0:
                return code
        elif choice == "2":
            enable_autostart()
            pause()
        elif choice == "3":
            disable_autostart()
            pause()
        elif choice == "4":
            return 0
        else:
            print("\nНеверный выбор. Попробуйте снова.")
            try:
                import time

                time.sleep(2)
            except Exception:
                pass


def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() in ("/autostart", "/run"):
        autostart = sys.argv[1].lower() == "/autostart"
        return run_messenger(autostart=autostart)
    return show_menu()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(1)
