"""Cloud-only character and portrait generation, with deterministic validation."""
from __future__ import annotations
import base64
import json
import logging
import aiohttp
from style import STYLE, AVATAR_STYLE, direction, repetition_issue

LOG = logging.getLogger('furrybot.engine')

STATS = ('str', 'dex', 'con', 'int', 'wis', 'cha')
CLASSES = ('Варвар', 'Бард', 'Жрец', 'Друид', 'Воин', 'Монах', 'Паладин', 'Следопыт', 'Плут', 'Чародей', 'Колдун', 'Волшебник')
ALIGNMENTS = ('Законопослушный добрый', 'Нейтральный добрый', 'Хаотичный добрый', 'Законопослушный нейтральный', 'Истинно нейтральный', 'Хаотичный нейтральный', 'Законопослушный злой', 'Нейтральный злой', 'Хаотичный злой')
COST = {8:0, 9:1, 10:2, 11:3, 12:4, 13:5, 14:7, 15:9}
LIMITS = {'name': 60, 'species': 60, 'background': 80, 'personality': 260, 'backstory': 850, 'trait_name': 60, 'trait': 260, 'appearance': 350}

class GenerationError(Exception):
    pass

SCHEMA = {
    'type':'object', 'additionalProperties':False,
    'properties': {**{key:{'type':'string'} for key in LIMITS},
        'class':{'type':'string','enum':list(CLASSES)},
        'alignment':{'type':'string','enum':list(ALIGNMENTS)},
        'stats':{'type':'object','additionalProperties':False,
                 'properties':{key:{'type':'integer','enum':list(COST)} for key in STATS}, 'required':list(STATS)}},
    'required':[*LIMITS, 'class', 'alignment', 'stats']}

SYSTEM = STYLE + """\nВерни только JSON по схеме. Русский язык, кроме appearance. species — конкретный вид по-русски. class и alignment из схемы. Статы — шесть целых 8–15, стоимость ровно 27: 8=0, 9=1, 10=2, 11=3, 12=4, 13=5, 14=7, 15=9 (надёжный способ: переставь числа 15,14,13,12,10,8 между шестью характеристиками в соответствии с классом; этот набор уже стоит 27). name/species/trait_name до 60 символов, background до 80, personality/trait до 260, backstory до 480, appearance до 350. background — нелепая профессия или статус, personality — странная привычка и недостаток, trait_name и trait — конкретная абсурдная способность без числовых бонусов. Вид, класс и реквизит согласованы с биографией. Не используй Markdown, ссылки, упоминания или реальные персональные данные."""

def validate(data: dict) -> dict:
    if not isinstance(data, dict) or set(data) != set(SCHEMA['required']):
        raise GenerationError('Некорректная структура персонажа')
    for key, limit in LIMITS.items():
        val = data[key]
        if not isinstance(val, str) or not val.strip() or len(val) > limit:
            raise GenerationError(f'Некорректное поле {key}: ожидается непустая строка до {limit} символов')
        data[key] = val.strip()
    if data['class'] not in CLASSES or data['alignment'] not in ALIGNMENTS:
        raise GenerationError('Неизвестный класс или мировоззрение')
    stats = data['stats']
    if not isinstance(stats, dict) or set(stats) != set(STATS):
        raise GenerationError('Некорректные характеристики')
    if any(type(v) is not int or v not in COST for v in stats.values()):
        raise GenerationError('Характеристики вне диапазона')
    total = sum(COST[v] for v in stats.values())
    if total != 27:
        raise GenerationError(f'Неверная сумма Point Buy: {total} вместо 27')
    return data


def extract_text(response: dict) -> dict:
    try:
        value = response['result']['response']
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError('not an object')
        return value
    except (KeyError, TypeError, ValueError) as exc:
        raise GenerationError('Некорректный JSON от Workers AI') from exc


def avatar_filename(img: bytes) -> str:
    if img.startswith(b'\xff\xd8\xff'):
        return 'fursona.jpg'
    if img.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'fursona.png'
    if img[:4] == b'RIFF' and img[8:12] == b'WEBP':
        return 'fursona.webp'
    raise GenerationError('Неизвестный формат аватара')


class CloudGenerator:
    def __init__(self, session: aiohttp.ClientSession, account_id: str, token: str, text_model: str, image_model: str, image_steps: int):
        self.session = session
        self.base = f'https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/'
        self.headers = {'Authorization': f'Bearer {token}'}
        self.text_model = text_model
        self.image_model = image_model
        self.image_steps = image_steps

    async def post(self, model: str, payload: dict, timeout: int) -> dict:
        try:
            async with self.session.post(self.base + model, headers=self.headers, json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False) as resp:
                if resp.status == 429:
                    raise GenerationError('Cloudflare ограничил запросы: возможно, исчерпана бесплатная квота. Сброс в 00:00 UTC')
                if resp.status in (401, 403):
                    raise GenerationError('Cloudflare: проверь токен, Account ID, права Workers AI и тариф')
                if resp.status != 200:
                    raise GenerationError(f'Cloudflare Workers AI: HTTP {resp.status}')
                result = await resp.json()
                if not isinstance(result, dict) or result.get('success') is not True:
                    raise GenerationError('Cloudflare Workers AI отклонил запрос; проверь квоту и модели в Dashboard')
                return result
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise GenerationError('Cloudflare Workers AI недоступен или вернул некорректный ответ') from exc

    async def character(self, recent: list[dict]) -> dict:
        correction = ''
        fallback = None
        last_error = None
        for attempt in range(1, 4):
            try:
                result = await self.post(self.text_model, {
                    'messages': [{'role':'system','content':SYSTEM}, {'role':'user','content':json.dumps(
                        {'recent_characters':recent[:8],'direction':direction(recent),'correction':correction}, ensure_ascii=False)}],
                    'response_format': {'type':'json_schema','json_schema':SCHEMA},
                    'max_tokens':2200,
                    'temperature':0.9,
                }, 90)
            except GenerationError as exc:
                if fallback is not None:
                    LOG.warning('Optional text improvement unavailable: %s; returning valid candidate', exc)
                    return fallback
                raise
            try:
                data = validate(extract_text(result))
            except GenerationError as exc:
                last_error = exc
                LOG.warning('Character attempt %s/3 rejected by schema: %s', attempt, exc)
                if fallback is not None:
                    return fallback
                correction = f'Повтори генерацию. Исправь ошибку: {exc}'
                continue

            issue = repetition_issue(data, recent)
            if len(data['backstory']) > 480:
                issue = 'Предыстория длиннее желательных 480 символов; сократи без потери шуток.' + (' '+issue if issue else '')
            if not issue:
                return data
            LOG.info('Character attempt %s/3 style warning: %s', attempt, issue)
            if fallback is not None:
                # Only one extra call for creative quality. Both cards are technically valid.
                return fallback
            fallback = data
            correction = f'Попробуй другой сюжет. Рекомендация по стилю: {issue}'
        if fallback is not None:
            return fallback
        raise GenerationError(f'Модель трижды вернула некорректную карточку. Последняя причина: {last_error}') from last_error

    async def portrait(self, data: dict) -> bytes:
        prompt = AVATAR_STYLE + data['appearance']
        result = await self.post(self.image_model, {'prompt':prompt,'steps':self.image_steps}, 180)
        try:
            img = base64.b64decode(result['result']['image'], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise GenerationError('Cloudflare не вернул аватар') from exc
        if not 12 <= len(img) <= 7 * 1024 * 1024:
            raise GenerationError('Некорректный размер аватара')
        avatar_filename(img)
        return img
