# -*- coding: utf-8 -*-
import os
import pwd
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / "venv" / "bin" / "python"
AUTOSTART = Path.home() / ".config" / "autostart" / "proton-messenger.desktop"
SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
SYSTEMD_UNIT = SYSTEMD_DIR / "proton-messenger.service"
SYSTEMD_SYSTEM_UNIT = Path("/etc/systemd/system/proton-messenger.service")
MODE_FILE = ROOT / ".proton_autostart_mode"
SH_FILE = ROOT / "start_proton.sh"
SERVICE_SCRIPT = ROOT / "proton_service.sh"
WAIT_SCRIPT = ROOT / "wait_for_postgres.sh"
LOG_FILE = ROOT / "autostart.log"
SERVICE_NAME = "proton-messenger.service"


def pause():
    try:
        input("\nНажмите Enter для выхода...")
    except (EOFError, KeyboardInterrupt):
        pass


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def run(cmd, **kwargs):
    print()
    return subprocess.run(cmd, cwd=ROOT, **kwargs)


def run_capture(cmd):
    try:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    except FileNotFoundError:
        return None


def service_run_user():
    return (
        os.environ.get("PROTON_USER")
        or os.environ.get("SUDO_USER")
        or os.environ.get("USER")
        or os.environ.get("LOGNAME")
        or "root"
    )


def is_root():
    return os.geteuid() == 0


def systemctl_cmd(*args):
    return (["systemctl", *args] if is_root() else ["sudo", "systemctl", *args])


def ensure_sh_executable():
    if SH_FILE.is_file():
        SH_FILE.chmod(SH_FILE.stat().st_mode | 0o111)


def write_service_script():
    """Wrapper для systemd: иначе systemd разрешает venv/bin/python → system python без пакетов."""
    root = str(ROOT.resolve())
    script = (
        "#!/bin/bash\n"
        f'ROOT="{root}"\n'
        'cd "$ROOT"\n'
        "export PYTHONUNBUFFERED=1\n"
        'exec "$ROOT/venv/bin/python" "$ROOT/proton.py"\n'
    )
    SERVICE_SCRIPT.write_text(script, encoding="utf-8")
    SERVICE_SCRIPT.chmod(0o755)

    wait = (
        "#!/bin/bash\n"
        "# Ждём PostgreSQL при boot (до 2 мин)\n"
        "for i in $(seq 1 60); do\n"
        "  pg_isready -q 2>/dev/null && exit 0\n"
        "  sleep 2\n"
        "done\n"
        "exit 0\n"
    )
    WAIT_SCRIPT.write_text(wait, encoding="utf-8")
    WAIT_SCRIPT.chmod(0o755)


def cleanup_login_only_autostart():
    """Убирает автозапуск, который срабатывает только после входа пользователя."""
    removed = False
    if AUTOSTART.is_file():
        AUTOSTART.unlink()
        removed = True
        print("  Убран GUI autostart (.desktop — только при входе в рабочий стол).")
    if SYSTEMD_UNIT.is_file():
        run(["systemctl", "--user", "disable", "--now", SERVICE_NAME])
        SYSTEMD_UNIT.unlink(missing_ok=True)
        removed = True
        print("  Убран user systemd (только после login-сессии).")
    return removed


def ensure_postgresql_boot():
    """PostgreSQL тоже должен стартовать при boot, иначе мессенджер не поднимется."""
    for svc in ("postgresql", "postgresql@16-main", "postgresql@15-main", "postgresql@14-main"):
        r = run_capture(systemctl_cmd("is-enabled", svc))
        if r and r.returncode == 0:
            print(f"  PostgreSQL автозагрузка: {svc} — OK")
            return True
    print("  Включаю postgresql в автозагрузку...")
    if run(systemctl_cmd("enable", "postgresql")).returncode == 0:
        print("  PostgreSQL добавлен в автозагрузку.")
        return True
    print("  Не удалось включить postgresql — проверьте: systemctl enable postgresql")
    return False


def autostart_on():
    return AUTOSTART.is_file()


def user_systemd_available():
    r = run_capture(["systemctl", "--user", "is-system-running"])
    if r is None:
        return False
    text = (r.stdout + r.stderr).lower()
    return "failed to connect to bus" not in text and "not been booted" not in text


def system_service_installed():
    r = run_capture(["systemctl", "is-enabled", SERVICE_NAME])
    return bool(r and r.returncode == 0)


def autostart_mode():
    if MODE_FILE.is_file():
        return MODE_FILE.read_text(encoding="utf-8").strip()
    if SYSTEMD_UNIT.is_file():
        return "user"
    if system_service_installed():
        return "system"
    return ""


def systemd_installed():
    return bool(autostart_mode())


def systemd_unit_text(run_user=None):
    lines = [
        "[Unit]",
        "Description=Proton Messenger",
        "After=network-online.target postgresql.service",
        "Wants=network-online.target postgresql.service",
        "StartLimitIntervalSec=0",
        "",
        "[Service]",
        "Type=simple",
    ]
    if run_user and run_user != "root":
        try:
            pw = pwd.getpwnam(run_user)
            import grp
            group = grp.getgrgid(pw.pw_gid).gr_name
            lines.extend([
                f"User={run_user}",
                f"Group={group}",
                f"WorkingDirectory={ROOT.resolve()}",
                f"Environment=HOME={pw.pw_dir}",
            ])
        except KeyError:
            lines.append(f"WorkingDirectory={ROOT.resolve()}")
    else:
        lines.append(f"WorkingDirectory={ROOT.resolve()}")
    lines.extend([
        f"ExecStartPre={WAIT_SCRIPT.resolve()}",
        f"ExecStart={SERVICE_SCRIPT.resolve()}",
        "Restart=always",
        "RestartSec=10",
        "Environment=PYTHONUNBUFFERED=1",
        "StandardOutput=journal",
        "StandardError=journal",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])
    return "\n".join(lines)


def enable_desktop_autostart():
    ensure_sh_executable()
    sh = str(SH_FILE.resolve())
    root = str(ROOT.resolve())
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=Proton Messenger\n"
        f"Exec={sh} --autostart\n"
        f"Path={root}\n"
        "Terminal=false\n"
        "Hidden=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Delay=15\n",
        encoding="utf-8",
    )
    print("\nАвтозагрузка GUI включена.")
    print(f"Файл: {AUTOSTART}")
    print("Срабатывает только при входе в графический рабочий стол.")


def enable_user_systemd():
    write_service_script()
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    SYSTEMD_UNIT.write_text(systemd_unit_text(), encoding="utf-8")
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    steps = [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", SERVICE_NAME],
        ["systemctl", "--user", "restart", SERVICE_NAME],
    ]
    for cmd in steps:
        if run(cmd).returncode != 0:
            print(f"\nОшибка: {' '.join(cmd)}")
            return False
    if user:
        run(["loginctl", "enable-linger", user])
    MODE_FILE.write_text("user", encoding="utf-8")
    print("\nАвтозапуск (user systemd) включён.")
    print(f"Юнит: {SYSTEMD_UNIT}")
    print("  systemctl --user status proton-messenger")
    return True


def enable_system_systemd():
    if not VENV_PY.is_file():
        print("\nСначала создайте venv (пункт 1 — запуск мессенджера).")
        return False
    write_service_script()
    cleanup_login_only_autostart()
    ensure_postgresql_boot()
    run_user = service_run_user()
    unit = systemd_unit_text(run_user=run_user)
    print(f"\nУстанавливаю /etc/systemd/system/{SERVICE_NAME}...")

    if is_root():
        SYSTEMD_SYSTEM_UNIT.write_text(unit, encoding="utf-8")
    else:
        tee = subprocess.run(
            ["sudo", "tee", str(SYSTEMD_SYSTEM_UNIT)],
            input=unit,
            text=True,
            cwd=ROOT,
        )
        if tee.returncode != 0:
            print("\nНе удалось записать unit-файл. Нужны права sudo.")
            return False

    for args in (
        ("daemon-reload",),
        ("enable", SERVICE_NAME),
        ("restart", SERVICE_NAME),
    ):
        if run(systemctl_cmd(*args)).returncode != 0:
            print(f"\nОшибка: systemctl {' '.join(args)}")
            return False

    MODE_FILE.write_text("system", encoding="utf-8")
    print("\nАвтозапуск включён — стартует при boot БЕЗ входа в SSH.")
    print(f"Пользователь сервиса: {run_user}")
    print(f"Unit: {SYSTEMD_SYSTEM_UNIT}")
    print("\nПроверка:")
    prefix = "" if is_root() else "sudo "
    print(f"  {prefix}systemctl is-enabled proton-messenger postgresql")
    print(f"  {prefix}systemctl status proton-messenger")
    print(f"  {prefix}journalctl -u proton-messenger -n 30 --no-pager")
    print("\nТест reboot: sudo reboot  →  после загрузки curl http://127.0.0.1:2200/")
    return True


def enable_systemd_autostart():
    if not VENV_PY.is_file():
        print("\nСначала создайте venv (пункт 1 — запуск мессенджера).")
        return False

    # На VPS всегда system-сервис — он работает без login
    print("\nСтавлю системный автозапуск (multi-user.target, без входа в систему)...")
    return enable_system_systemd()


def disable_autostart():
    removed = False
    if AUTOSTART.is_file():
        AUTOSTART.unlink()
        removed = True
        print("\nАвтозагрузка GUI отключена.")

    mode = autostart_mode()
    if mode == "user" or SYSTEMD_UNIT.is_file():
        run(["systemctl", "--user", "disable", "--now", SERVICE_NAME])
        SYSTEMD_UNIT.unlink(missing_ok=True)
        removed = True
        print("User systemd отключён.")

    if mode == "system" or system_service_installed():
        run(systemctl_cmd("disable", "--now", SERVICE_NAME))
        if is_root():
            SYSTEMD_SYSTEM_UNIT.unlink(missing_ok=True)
        else:
            run(["sudo", "rm", "-f", str(SYSTEMD_SYSTEM_UNIT)])
        run(systemctl_cmd("daemon-reload"))
        removed = True
        print("System systemd отключён.")

    if MODE_FILE.is_file():
        MODE_FILE.unlink()
    if not removed:
        print("\nАвтозагрузка уже была выключена.")


def _systemctl_state(scope, prop):
    if scope == "system":
        r = run_capture(systemctl_cmd(prop, SERVICE_NAME))
    else:
        r = run_capture(["systemctl", "--user", prop, SERVICE_NAME])
    if not r:
        return "—"
    if r.returncode == 0:
        return r.stdout.strip()
    return (r.stderr or r.stdout or "—").strip().splitlines()[0][:80]


def show_status():
    print("\n========================================")
    print("       Проверка автозапуска")
    print("========================================\n")

    print("1) GUI (.desktop):", "ВКЛ" if autostart_on() else "ВЫКЛ")

    mode = autostart_mode()
    print("\n2) systemd:", "ВКЛ" if mode else "ВЫКЛ", f"({mode or 'не настроен'})")
    if mode == "user":
        print(f"   {SYSTEMD_UNIT}")
        print("   enabled:", _systemctl_state("user", "is-enabled"))
        print("   active:", _systemctl_state("user", "is-active"))
    elif mode == "system":
        print(f"   {SYSTEMD_SYSTEM_UNIT}")
        print("   enabled:", _systemctl_state("system", "is-enabled"))
        print("   active:", _systemctl_state("system", "is-active"))

    print("\n3) user systemd bus:", "OK" if user_systemd_available() else "нет (норма для VPS)")

    r_pg = run_capture(systemctl_cmd("is-enabled", "postgresql"))
    pg = r_pg.stdout.strip() if r_pg and r_pg.returncode == 0 else "не включён"
    print(f"\n4) PostgreSQL автозагрузка: {pg}")

    print(f"\n5) Лог: {LOG_FILE}")
    if LOG_FILE.is_file():
        for line in LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-10:]:
            print("  ", line)

    print("\n6) Команды проверки:")
    prefix = "" if is_root() else "sudo "
    if mode == "system":
        print(f"   {prefix}systemctl status proton-messenger")
        print(f"   {prefix}journalctl -u proton-messenger -n 50 --no-pager")
    else:
        print("   systemctl --user status proton-messenger")
        print("   journalctl --user -u proton-messenger -n 50 --no-pager")
    print("   curl -s -o /dev/null -w '%{http_code}\\n' http://127.0.0.1:2200/")
    print()


def setup_venv():
    if not shutil.which("python3"):
        print("Ошибка: python3 не найден.")
        return False
    if not (ROOT / "requirements.txt").is_file():
        print("Ошибка: requirements.txt не найден.")
        return False
    if not (ROOT / "linux_offline").is_dir():
        print("Ошибка: папка linux_offline не найдена.")
        return False

    print("venv не найден. Создаю окружение...")
    if run(["python3", "-m", "venv", str(ROOT / "venv")]).returncode != 0:
        return False

    print("Устанавливаю зависимости из linux_offline...")
    if run([
        str(VENV_PY), "-m", "pip", "install",
        "--no-index", "--find-links=linux_offline",
        "-r", "requirements.txt",
    ]).returncode != 0:
        print("Ошибка установки зависимостей.")
        return False
    print("\nОкружение готово.")
    return True


def run_messenger(autostart=False):
    if autostart:
        log("autostart: запуск")
    print("\nЗапуск Proton Messenger...")
    if not VENV_PY.is_file() and not setup_venv():
        if autostart:
            log("autostart: venv не найден")
        return 1
    proton = ROOT / "proton.py"
    if not proton.is_file():
        print("Ошибка: proton.py не найден.")
        if autostart:
            log("autostart: proton.py не найден")
        return 1
    code = run([str(VENV_PY), str(proton)]).returncode
    if autostart:
        log(f"autostart: завершён с кодом {code}")
    elif not code:
        print("\nМессенджер остановлен.")
    else:
        print(f"\nМессенджер остановлен (код {code}).")
    return code


def menu():
    while True:
        os.system("clear")
        mode = autostart_mode()
        systemd_label = {"user": "user", "system": "system"}.get(mode, "ВЫКЛ")
        print("========================================")
        print("        Proton Messenger (Linux)")
        print("========================================\n")
        print(f" GUI автозагрузка:     {'ВКЛ' if autostart_on() else 'ВЫКЛ'}")
        print(f" systemd автозапуск:   {systemd_label}")
        print("\n 1. Запустить мессенджер")
        print(" 2. Автозагрузка GUI (только Ubuntu Desktop, НЕ для VPS)")
        print(" 3. Автозапуск при boot (systemd, без входа в SSH)")
        print(" 4. Отключить автозагрузку")
        print(" 5. Проверить статус")
        print(" 6. Выход\n")
        try:
            choice = input("Выберите действие (1-6): ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice == "1":
            run_messenger()
            pause()
        elif choice == "2":
            enable_desktop_autostart()
            pause()
        elif choice == "3":
            enable_systemd_autostart()
            pause()
        elif choice == "4":
            disable_autostart()
            pause()
        elif choice == "5":
            show_status()
            pause()
        elif choice == "6":
            return 0


def main():
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg in ("/autostart", "/run", "--autostart", "--run"):
            return run_messenger(autostart=arg in ("/autostart", "--autostart"))
        if arg in ("/status", "--status"):
            show_status()
            return 0
    return menu()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(1)
