#!/usr/bin/env python3
"""Host CLI; secrets are entered interactively, never as command arguments."""
import argparse
import fcntl
import getpass
import grp
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from config import CONFIG, REQUIRED, read_config

APP=Path('/opt/furrybot')
VENVS=APP/'.venvs'
UNIT='furrybot.service'
UNIT_PATH=Path('/etc/systemd/system/furrybot.service')
VERSION='2.3.1'

def run(*args, check=True, capture=False, cwd=None):
    return subprocess.run([str(a) for a in args],check=check,cwd=cwd, text=True,stdout=subprocess.PIPE if capture else None)

def git(*args):
    return run('git','-C',APP,*args,capture=True).stdout.strip()

def root():
    if os.geteuid()!=0:
        raise PermissionError('Для этого действия: sudo furrybot (или sudo furrybot --команда).')

def save_config(cfg):
    root()
    CONFIG.parent.mkdir(mode=0o750,parents=True,exist_ok=True)
    fd,path=tempfile.mkstemp(prefix='.config-',dir=CONFIG.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            os.fchmod(f.fileno(),0o640)
            os.fchown(f.fileno(),0,grp.getgrnam('furrybot').gr_gid)
            json.dump(cfg,f,ensure_ascii=False,indent=2)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(path,CONFIG)
    finally:
        if os.path.exists(path): os.unlink(path)

def secret_menu():
    root()
    if not sys.stdin.isatty(): raise ValueError('Ввод секретов требует интерактивного терминала.')
    cfg=read_config()
    for key,label in [('DISCORD_TOKEN','Discord bot token'),('CLOUDFLARE_API_TOKEN','Cloudflare Workers AI API token')]:
        state='задан' if cfg[key] else 'не задан'
        value=getpass.getpass(f'{label} [{state}; Enter — оставить]: ').strip()
        if value: cfg[key]=value
    while True:
        value=input(f"Cloudflare Account ID [{cfg['CLOUDFLARE_ACCOUNT_ID'] or 'не задан'}; Enter — оставить]: ").strip()
        if not value: break
        if re.fullmatch(r'[0-9a-fA-F]{32}',value):
            cfg['CLOUDFLARE_ACCOUNT_ID']=value; break
        print('Нужен 32-значный hex ID.')
    save_config(cfg)
    print('Настройки сохранены. Для применения запусти или перезапусти сервис.')

def settings_menu():
    root()
    cfg=read_config()
    for key in ('TEXT_MODEL','IMAGE_MODEL','IMAGE_STEPS','MAX_PARALLEL','ADMIN_ROLE','MEMBER_ROLE','CHANNEL_NAME'):
        value=input(f'{key} [{cfg[key]}]: ').strip()
        if value:
            if key=='MAX_PARALLEL' and (not value.isdigit() or not 1<=int(value)<=10):
                print('Допустимо 1–10; оставлено прежнее значение.'); continue
            if key=='IMAGE_STEPS' and (not value.isdigit() or not 1<=int(value)<=8):
                print('Допустимо 1–8; оставлено прежнее значение.'); continue
            cfg[key]=value
    save_config(cfg)
    print('Настройки сохранены. Перезапусти сервис. Уже зарегистрированные ID ролей сохраняются.')

def start_or_restart(action):
    root()
    cfg=read_config()
    missing=[k for k in REQUIRED if not cfg[k]]
    if missing: raise ValueError('Сначала настрой: '+', '.join(missing))
    run('systemctl',action,UNIT)

def current_active():
    return run('systemctl','is-active','--quiet',UNIT,check=False).returncode==0

def check_update():
    root()
    with (VENVS/'update.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        old=git('rev-parse','HEAD')
        git('fetch','origin','main')
        new=git('rev-parse','FETCH_HEAD')
        print('Установлена:',old[:12], 'Доступна:',new[:12])
        if old==new: print('Обновлений нет.')
        elif run('git','-C',APP,'merge-base','--is-ancestor',old,new,check=False).returncode: print('Ветки разошлись; автоматическое обновление невозможно.')
        else: print('Есть обновление. sudo furrybot --update')

def switch_venv(rev):
    temp=VENVS/'current.next'
    temp.unlink(missing_ok=True)
    temp.symlink_to(rev)
    os.replace(temp,VENVS/'current')

def update():
    root()
    with (VENVS/'update.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if git('status','--porcelain'):
            raise ValueError('Рабочее дерево изменено. Обновление отменено, изменения сохранены.')
        old=git('rev-parse','HEAD')
        git('fetch','origin','main')
        new=git('rev-parse','FETCH_HEAD')
        if new==old: print('Уже актуальная версия.'); return
        if run('git','-C',APP,'merge-base','--is-ancestor',old,new,check=False).returncode:
            raise ValueError('Ветки разошлись; требуется ручное решение.')
        was_active=current_active()
        newenv=VENVS/new
        # Construct dependencies before touching the installed source or running service.
        run(sys.executable,'-m','venv',newenv)
        with tempfile.TemporaryDirectory() as tmp:
            req=Path(tmp)/'requirements.txt'
            req.write_text(git('show',new+':requirements.txt')+'\n',encoding='utf-8')
            run(newenv/'bin/python','-m','pip','install','--disable-pip-version-check','-r',req)
        oldenv=os.readlink(VENVS/'current')
        oldunit=UNIT_PATH.read_bytes()
        try:
            if was_active: run('systemctl','stop',UNIT)
            git('merge','--ff-only',new)
            run(newenv/'bin/python','-m','unittest','discover','-s','tests',cwd=APP)
            switch_venv(new)
            UNIT_PATH.write_bytes((APP/'furrybot.service').read_bytes())
            run('systemctl','daemon-reload')
            if was_active:
                run('systemctl','start',UNIT)
                time.sleep(3)
                if not current_active(): raise RuntimeError('Сервис не запустился после обновления')
        except BaseException:
            print('Ошибка обновления. Восстанавливаю предыдущий код и окружение.',file=sys.stderr)
            run('systemctl','stop',UNIT,check=False)
            git('reset','--keep',old)
            switch_venv(oldenv)
            UNIT_PATH.write_bytes(oldunit)
            run('systemctl','daemon-reload')
            if was_active: run('systemctl','start',UNIT)
            raise
        print(f'Обновлено: {old[:12]} → {new[:12]}')

def status():
    run('systemctl','status',UNIT,'--no-pager',check=False)

def logs():
    run('journalctl','-u',UNIT,'-n','100','-f',check=False)

ACTIONS={'status':status,'start':lambda:start_or_restart('start'),'stop':lambda:(root(),run('systemctl','stop',UNIT)),
         'restart':lambda:start_or_restart('restart'),'logs':logs,'secret':secret_menu,'config':settings_menu,
         'check-update':check_update,'update':update,'version':lambda:print('Furrybot '+VERSION)}
MENU=[('status','Статус'),('start','Запустить'),('stop','Остановить'),('restart','Перезапустить'),('logs','Журнал'),('secret','Задать секреты'),('config','Настройки'),('check-update','Проверить обновления'),('update','Обновить'),('version','Версия')]

def execute(action):
    try:
        ACTIONS[action]()
        return 0
    except KeyboardInterrupt:
        print('\nПрервано.'); return 130
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError) as exc:
        print(f'Ошибка: {exc}',file=sys.stderr); return 1

def main():
    parser=argparse.ArgumentParser(prog='furrybot',description='Управление нативным сервисом Furrybot')
    parser.add_argument('command',nargs='?',choices=ACTIONS)
    group=parser.add_mutually_exclusive_group()
    for action in ACTIONS:
        group.add_argument('--'+action,dest='flag',action='store_const',const=action)
    args=parser.parse_args()
    if args.flag and args.command: parser.error('Укажи одну команду.')
    if args.flag or args.command: return execute(args.flag or args.command)
    if not sys.stdin.isatty(): parser.print_help(); return 0
    while True:
        print('\nFurrybot '+VERSION)
        for i,(_,label) in enumerate(MENU,1): print(f'  {i}. {label}')
        print('  0. Выход')
        try: choice=input('> ').strip()
        except (EOFError,KeyboardInterrupt): return 0
        if choice=='0': return 0
        if choice.isdigit() and 1<=int(choice)<=len(MENU): execute(MENU[int(choice)-1][0])
        else: print('Неизвестный пункт.')

if __name__=='__main__':
    sys.exit(main())
