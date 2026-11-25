"""Configuration shared by the bot and the native CLI."""
import json
import os
from pathlib import Path

CONFIG = Path('/etc/furrybot/config.json')
DEFAULTS = {'DISCORD_TOKEN':'', 'OPENAI_API_KEY':'', 'DISCORD_GUILD_ID':'',
            'TEXT_MODEL':'gpt-4.1-mini','IMAGE_MODEL':'gpt-image-1.5',
            'IMAGE_QUALITY':'medium','MAX_PARALLEL':'2',
            'DATABASE_PATH':'/var/lib/furrybot/fursonas.sqlite3',
            'ADMIN_ROLE':'Его пушистое величество','MEMBER_ROLE':'Пушистый ребёнок природы',
            'CHANNEL_NAME':'фуррификация'}
REQUIRED = ('DISCORD_TOKEN','OPENAI_API_KEY','DISCORD_GUILD_ID')

def read_config(path=CONFIG):
    values = DEFAULTS.copy()
    if path.exists():
        raw = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(raw,dict):
            raise ValueError('config.json должен содержать объект')
        values.update({k:str(v) for k,v in raw.items() if k in DEFAULTS})
    return values

def runtime_config():
    values=read_config()
    values.update({k:os.environ[k] for k in DEFAULTS if k in os.environ})
    missing=[k for k in REQUIRED if not values[k].strip()]
    if missing:
        raise ValueError('Не настроены: '+', '.join(missing))
    if not values['DISCORD_GUILD_ID'].isdigit() or not 0 < int(values['DISCORD_GUILD_ID']) < 2**63:
        raise ValueError('DISCORD_GUILD_ID: ожидается числовой ID сервера')
    if not 1 <= int(values['MAX_PARALLEL']) <= 10:
        raise ValueError('MAX_PARALLEL: от 1 до 10')
    return values
