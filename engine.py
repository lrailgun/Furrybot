"""Cloud-only character and portrait generation, with deterministic validation."""
from __future__ import annotations
import base64
import json
import logging
import time
from pathlib import Path
import aiohttp
from style import STYLE, PITCH_STYLE, AVATAR_STYLE, direction, repetition_issue, unique_names, normalized_name, NAME_DEMOS

LOG = logging.getLogger('furrybot.engine')
KLEIN_MODEL = '@cf/black-forest-labs/flux-2-klein-4b'
KLEIN_QUALITY_MODEL = '@cf/black-forest-labs/flux-2-klein-9b'
REFERENCE_MODELS = (KLEIN_MODEL,KLEIN_QUALITY_MODEL)
STYLE_REFERENCES = tuple(Path(__file__).resolve().parent / 'assets' / f'furry-style-{i}.jpg' for i in (1, 2))
MIN_STORY_LENGTH = 400
PREFERRED_STORY_LENGTH = 750

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

PITCH_SCHEMA = {
    'type':'object', 'additionalProperties':False,
    'properties':{'pitches':{'type':'array','items':{'type':'string'},'minItems':3,'maxItems':3},
                  'names':{'type':'array','items':{'type':'string'},'minItems':5,'maxItems':5}},
    'required':['pitches','names'],
}

SYSTEM = STYLE + """\nВерни только JSON по схеме. Русский язык, кроме appearance. species — конкретный вид по-русски. class и alignment из схемы. Статы — шесть целых 8–15, стоимость ровно 27: 8=0, 9=1, 10=2, 11=3, 12=4, 13=5, 14=7, 15=9 (надёжный способ: переставь числа 15,14,13,12,10,8 между шестью характеристиками в соответствии с классом; этот набор уже стоит 27). name/species/trait_name до 60 символов, background до 80, personality/trait до 260, backstory 400–850 (цель 450–650, оставь запас до максимума и закончи сюжет), appearance до 350. background — короткая профессия или статус, personality — одна конкретная смешная черта (до двух коротких фраз), trait_name и trait — короткая комическая способность без числовых бонусов. Вид, класс и реквизит согласованы с биографией. Не используй Markdown, ссылки, упоминания или реальные персональные данные."""

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

    async def post(self, model: str, payload: dict | aiohttp.FormData, timeout: int) -> dict:
        started=time.monotonic()
        LOG.info('Cloudflare request started: model=%s timeout_s=%s',model,timeout)
        try:
            body = {'data':payload} if isinstance(payload,aiohttp.FormData) else {'json':payload}
            async with self.session.post(self.base + model, headers=self.headers, **body,
                    timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False) as resp:
                LOG.info('Cloudflare response headers: model=%s status=%s elapsed_s=%.1f',model,resp.status,time.monotonic()-started)
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
        except TimeoutError as exc:
            raise GenerationError(f'Cloudflare Workers AI: модель {model} не ответила за {timeout} секунд') from exc
        except (aiohttp.ClientError, ValueError) as exc:
            raise GenerationError('Cloudflare Workers AI недоступен или вернул некорректный ответ') from exc
        finally:
            LOG.info('Cloudflare request ended: model=%s elapsed_s=%.1f',model,time.monotonic()-started)

    async def draft_pitches(self, creative_direction: dict) -> tuple[list[str],list[str]]:
        result = await self.post(self.text_model, {
            'messages': [{'role':'system','content':PITCH_STYLE},
                         {'role':'user','content':json.dumps(creative_direction,ensure_ascii=False)}],
            'response_format': {'type':'json_schema','json_schema':PITCH_SCHEMA},
            'max_tokens':950, 'temperature':1.0,
        }, 90)
        try:
            idea_data = extract_text(result)
            names = unique_names(idea_data.get('names'),creative_direction.get('avoid_names',[]))
            values = idea_data.get('pitches')
            if not isinstance(values,list):
                raise GenerationError('Некорректные комедийные завязки')
            pitches = list(dict.fromkeys(v.strip() for v in values
                           if isinstance(v,str) and 20 <= len(v.strip()) <= 350))
            if not pitches:
                raise GenerationError('Пустые комедийные завязки')
            return pitches[:3],names
        except GenerationError as exc:
            LOG.warning('Pitch response invalid: %s; generating card directly',exc)
            return [],[]

    async def character(self, recent: list[dict]) -> dict:
        creative_direction = direction(recent)
        pitches,names = await self.draft_pitches(creative_direction)
        used_names={normalized_name(n) for n in (*creative_direction['avoid_names'],*NAME_DEMOS)}
        candidate_names={normalized_name(n) for n in names}
        identities = [{k:row[k] for k in ('name','species','class') if k in row}
                      for row in recent[:8]]
        correction = ''
        fallback = None
        last_error = None
        for attempt in range(1, 4):
            try:
                result = await self.post(self.text_model, {
                    'messages': [{'role':'system','content':SYSTEM}, {'role':'user','content':json.dumps(
                        {'recent_characters':identities,'direction':creative_direction,'comic_pitches':pitches,'name_candidates':names,'name_style':creative_direction['name_style'],'correction':correction}, ensure_ascii=False)}],
                    'response_format': {'type':'json_schema','json_schema':SCHEMA},
                    'max_tokens':2200,
                    'temperature':0.85,
                }, 90)
            except GenerationError as exc:
                if fallback is not None:
                    LOG.warning('Optional text improvement unavailable: %s; returning valid candidate', exc)
                    return fallback
                raise
            try:
                data = validate(extract_text(result))
                if normalized_name(data['name']) in used_names:
                    raise GenerationError('Имя уже встречалось в недавних генерациях. Выбери другое имя и согласуй с ним биографию.')
                if candidate_names and normalized_name(data['name']) not in candidate_names:
                    raise GenerationError('Выбери одно из предложенных комических имён name_candidates, не подменяй его обычным именем.')
                if len(data['backstory']) < MIN_STORY_LENGTH:
                    raise GenerationError(f'Предыстория слишком короткая: {len(data["backstory"])} символов; нужно минимум {MIN_STORY_LENGTH}. Разверни конфликт и добавь конкретное последствие и панчлайн.')
                if not data['backstory'].rstrip('»”\"\')]').endswith(('.', '!', '?')):
                    raise GenerationError('Предыстория оборвана: закончи последнюю мысль полноценным предложением с панчлайном.')
            except GenerationError as exc:
                last_error = exc
                LOG.warning('Character attempt %s/3 rejected by schema: %s', attempt, exc)
                if fallback is not None:
                    return fallback
                correction = f'Повтори генерацию. Исправь ошибку: {exc}'
                continue

            issue = repetition_issue(data, recent)
            if len(data['backstory']) > PREFERRED_STORY_LENGTH:
                issue = f'Предыстория длиннее желательных {PREFERRED_STORY_LENGTH} символов; сократи без потери шуток.' + (' '+issue if issue else '')
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

    def reference_form(self, data: dict) -> aiohttp.FormData:
        form = aiohttp.FormData()
        prompt = (
            'Create a new character portrait as a companion illustration to image 0 and image 1, '
            'matching their exaggerated cartoon proportions, expressive shape design, tapered ink linework, layered flat fur shapes, '
            'graphic cel shadows, pale keyline and vintage print texture. Match the reference cartoon stylization, not realism or a simple logo. '
            'Use images 0 and 1 ONLY as a reference for drawing technique and framing. '
            'The subject is a different individual of the requested species, with its own face, outfit, expression and palette. '
            'Requested species (Russian): ' + data['species'] + '. '
            + AVATAR_STYLE + data['appearance'] +
            '. Final composition: the large head dominates the square, chest cut by the bottom edge, '
            'shoulders reach both sides. Match the reference drawing style rather than a generic cartoon animal.'
        )
        form.add_field('prompt',prompt)
        form.add_field('width','1024')
        form.add_field('height','1024')
        for index,path in enumerate(STYLE_REFERENCES):
            try:
                reference = path.read_bytes()
            except OSError as exc:
                raise GenerationError('Не найден каноничный референс аватара; проверь assets и обновление Furrybot') from exc
            if not reference.startswith(b'\xff\xd8\xff'):
                raise GenerationError('Каноничный референс повреждён; проверь assets')
            form.add_field(f'input_image_{index}',reference,filename=path.name,content_type='image/jpeg')
        return form

    async def portrait(self, data: dict) -> bytes:
        if self.image_model in REFERENCE_MODELS:
            payload = self.reference_form(data)
        else:
            prompt = AVATAR_STYLE + data['appearance']
            payload = {'prompt':prompt,'steps':self.image_steps}
        LOG.info('Avatar generation: model=%s, reference_count=%s, requested_size=%s',
                 self.image_model, len(STYLE_REFERENCES) if self.image_model in REFERENCE_MODELS else 0,
                 '1024x1024' if self.image_model in REFERENCE_MODELS else 'model default')
        result = await self.post(self.image_model, payload, 180)
        try:
            img = base64.b64decode(result['result']['image'], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise GenerationError('Cloudflare не вернул аватар') from exc
        if not 12 <= len(img) <= 7 * 1024 * 1024:
            raise GenerationError('Некорректный размер аватара')
        avatar_filename(img)
        return img
