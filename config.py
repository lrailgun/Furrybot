"""Configuration shared by the bot and the native CLI."""
import json
import os
import re
from pathlib import Path

CONFIG = Path('/etc/furrybot/config.json')
DEFAULTS = {'DISCORD_TOKEN':'', 'CLOUDFLARE_ACCOUNT_ID':'', 'CLOUDFLARE_API_TOKEN':'', 'DISCORD_GUILD_ID':'',
            'TEXT_MODEL':'@cf/meta/llama-3.3-70b-instruct-fp8-fast','IMAGE_MODEL':'@cf/black-forest-labs/flux-1-schnell',
            'IMAGE_STEPS':'4','MAX_PARALLEL':'2',
            'DATABASE_PATH':'/var/lib/furrybot/fursonas.sqlite3',
            'ADMIN_ROLE':'Его пушистое величество','MEMBER_ROLE':'Пушистый ребёнок природы',
            'CHANNEL_NAME':'фуррификация'}
REQUIRED = ('DISCORD_TOKEN','CLOUDFLARE_ACCOUNT_ID','CLOUDFLARE_API_TOKEN','DISCORD_GUILD_ID')

def read_config(path=CONFIG):
    values = DEFAULTS.copy()
    if path.exists():
        raw = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(raw,dict):
            raise ValueError('config.json должен содержать объект')
        values.update({k:str(v) for k,v in raw.items() if k in DEFAULTS})
        if values['TEXT_MODEL'] == 'gpt-4.1-mini':
            values['TEXT_MODEL'] = DEFAULTS['TEXT_MODEL']
        if values['IMAGE_MODEL'] == 'gpt-image-1.5':
            values['IMAGE_MODEL'] = DEFAULTS['IMAGE_MODEL']
    return values

def runtime_config():
    values=read_config()
    values.update({k:os.environ[k] for k in DEFAULTS if k in os.environ})
    missing=[k for k in REQUIRED if not values[k].strip()]
    if missing:
        raise ValueError('Не настроены: '+', '.join(missing))
    if not values['DISCORD_GUILD_ID'].isdigit() or not 0 < int(values['DISCORD_GUILD_ID']) < 2**63:
        raise ValueError('DISCORD_GUILD_ID: ожидается числовой ID сервера')
    if not re.fullmatch(r'[0-9a-fA-F]{32}', values['CLOUDFLARE_ACCOUNT_ID']):
        raise ValueError('CLOUDFLARE_ACCOUNT_ID: ожидается 32-значный hex ID')
    if not 1 <= int(values['IMAGE_STEPS']) <= 8:
        raise ValueError('IMAGE_STEPS: от 1 до 8')
    if not 1 <= int(values['MAX_PARALLEL']) <= 10:
        raise ValueError('MAX_PARALLEL: от 1 до 10')
    return values
