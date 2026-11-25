"""Discord entrypoint. Run with python bot.py or Docker Compose."""
from __future__ import annotations
import asyncio
import io
import logging
import os
from collections import Counter

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

from engine import CloudGenerator, GenerationError, STATS
from storage import Store

LOG = logging.getLogger('furrification')
STAT_LABELS = dict(zip(STATS, ('СИЛ', 'ЛОВ', 'ТЕЛ', 'ИНТ', 'МДР', 'ХАР')))


def env_int(key: str, default: int, minimum: int = 1, maximum: int = 100000) -> int:
    value = int(os.getenv(key, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f'{key}: допустимо от {minimum} до {maximum}')
    return value


def escape(value: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))


def card(data: dict, avatar: bytes, member: discord.abc.User):
    embed = discord.Embed(title=escape(data['name'])[:256],
        description=f"**{escape(data['species'])} · {escape(data['class'])} · уровень 1**\n{escape(data['alignment'])}",
        colour=0x9B7AD1)
    embed.set_author(name=member.display_name[:256])
    embed.set_thumbnail(url='attachment://fursona.webp')
    stats = data['stats']
    for key in STATS:
        score = stats[key]
        embed.add_field(name=STAT_LABELS[key], value=f'**{score}** ({(score-10)//2:+d})', inline=True)
    embed.add_field(name='Происхождение', value=escape(data['background'])[:1024], inline=False)
    embed.add_field(name='Характер', value=escape(data['personality'])[:1024], inline=False)
    embed.add_field(name='Предыстория', value=escape(data['backstory'])[:1024], inline=False)
    embed.add_field(name=('Особенность: '+escape(data['trait_name']))[:256], value=escape(data['trait'])[:1024], inline=False)
    embed.set_footer(text='Point Buy 27 · базовые статы · сюжетная особенность не является правилом D&D')
    return embed, discord.File(io.BytesIO(avatar), filename='fursona.webp')


class FurBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.none(), allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.guild_id = env_int('DISCORD_GUILD_ID', 0, 1, 2**63-1)
        self.daily_limit = env_int('DAILY_LIMIT', 20)
        self.cooldown = env_int('COOLDOWN_SECONDS', 600, 0, 604800)
        self.max_parallel = env_int('MAX_PARALLEL', 2, 1, 10)
        self.active: set[tuple[int,int]] = set()
        self.store = Store(os.getenv('DATABASE_PATH', 'data/fursonas.sqlite3'))
        self.session = None
        self.cloud = None
        self.install_commands()

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.cloud = CloudGenerator(self.session, os.environ['OPENAI_API_KEY'],
            os.getenv('TEXT_MODEL', 'gpt-4.1-mini'), os.getenv('IMAGE_MODEL', 'gpt-image-1.5'),
            os.getenv('IMAGE_QUALITY', 'medium'))
        guild = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=guild)
        registered = await self.tree.sync(guild=guild)
        LOG.info('Registered %d commands for configured guild', len(registered))

    async def on_ready(self):
        LOG.info('Discord connected as %s', self.user)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        try:
            await super().close()
        finally:
            self.store.close()

    def allowed(self, inter: discord.Interaction) -> bool:
        return inter.guild_id == self.guild_id

    async def generate(self, inter: discord.Interaction, replace: bool):
        if not self.allowed(inter):
            await inter.response.send_message('Команда доступна только на настроенном сервере.', ephemeral=True)
            return
        key = (inter.guild_id, inter.user.id)
        if key in self.active:
            await inter.response.send_message('Твоя фурсона уже генерируется.', ephemeral=True)
            return
        existing = self.store.get(*key)
        if existing and not replace:
            embed, attachment = card(*existing, inter.user)
            await inter.response.send_message('Ты уже фуррифицирован. Для нового персонажа — `/reroll`.', embed=embed, file=attachment, ephemeral=True)
            return
        if replace and not existing:
            await inter.response.send_message('Сначала создай персонажа через `/furrify`.', ephemeral=True)
            return
        if len(self.active) >= self.max_parallel:
            await inter.response.send_message('Все слоты генерации заняты. Попробуй немного позже.', ephemeral=True)
            return
        # No await between admission checks, quota reservation, and registering the job.
        denied = self.store.reserve(*key, self.daily_limit, self.cooldown)
        if denied:
            await inter.response.send_message(denied, ephemeral=True)
            return
        self.active.add(key)
        try:
            await inter.response.defer(thinking=True, ephemeral=False)
            data = await self.cloud.character(self.store.recent(inter.guild_id))
            await inter.edit_original_response(content='Персонаж создан. Облачная модель рисует аватар…')
            avatar = await self.cloud.portrait(data)
            # Save only a complete result. A failed reroll never replaces the existing character.
            self.store.save(*key, data, avatar)
            embed, attachment = card(data, avatar, inter.user)
            await inter.edit_original_response(content='Фуррификация завершена!', embed=embed, attachments=[attachment])
        except GenerationError as exc:
            LOG.warning('Generation failed: %s', exc)
            await inter.edit_original_response(content=f'Генерация не завершена: {exc}. Предыдущая фурсона сохранена, если она была.')
        except Exception:
            LOG.exception('Unexpected generation failure')
            if inter.response.is_done():
                try:
                    await inter.edit_original_response(content='Не удалось завершить команду. Проверь `/fursona`: сохранённый персонаж мог уже обновиться.')
                except discord.HTTPException:
                    pass
            else:
                await inter.response.send_message('Внутренняя ошибка.', ephemeral=True)
        finally:
            self.active.discard(key)

    def install_commands(self):
        tree = self.tree

        @tree.command(name='furrify', description='Создать собственную фурсону с классом, статами и аватаром')
        @app_commands.guild_only()
        async def furrify(inter: discord.Interaction):
            await self.generate(inter, replace=False)

        @tree.command(name='reroll', description='Заменить свою фурсону новым персонажем')
        @app_commands.guild_only()
        @app_commands.describe(confirm='Подтверди замену старого персонажа')
        async def reroll(inter: discord.Interaction, confirm: bool):
            if not confirm:
                await inter.response.send_message('Замена отменена.', ephemeral=True)
                return
            await self.generate(inter, replace=True)

        @tree.command(name='fursona', description='Посмотреть карточку свою или другого участника')
        @app_commands.guild_only()
        @app_commands.describe(user='Участник (если не указан — ты)')
        async def fursona(inter: discord.Interaction, user: discord.Member | None = None):
            member = user or inter.user
            existing = self.store.get(inter.guild_id, member.id)
            if not existing:
                await inter.response.send_message('У этого участника пока нет фурсоны.', ephemeral=True)
                return
            embed, attachment = card(*existing, member)
            await inter.response.send_message(embed=embed, file=attachment)

        @tree.command(name='avatar', description='Получить оригинальный квадратный аватар своей фурсоны')
        @app_commands.guild_only()
        async def avatar(inter: discord.Interaction):
            existing = self.store.get(inter.guild_id, inter.user.id)
            if not existing:
                await inter.response.send_message('Сначала используй `/furrify`.', ephemeral=True)
                return
            await inter.response.send_message(file=discord.File(io.BytesIO(existing[1]), filename='fursona.webp'), ephemeral=True)

        @tree.command(name='species', description='Статистика видов фурсон сервера')
        @app_commands.guild_only()
        async def species(inter: discord.Interaction):
            roster = self.store.roster(inter.guild_id)
            if not roster:
                await inter.response.send_message('Пока никто не фуррифицирован.', ephemeral=True)
                return
            counts = Counter(data['species'].casefold() for _, data in roster)
            lines = [f'{escape(name.capitalize())} — **{count}**' for name,count in counts.most_common(20)]
            embed = discord.Embed(title='Виды фурсон', description='\n'.join(lines)[:4000], colour=0x9B7AD1)
            embed.set_footer(text=f'Персонажей: {len(roster)} · видов: {len(counts)} · до 20 наиболее частых видов')
            await inter.response.send_message(embed=embed)

        @tree.command(name='roster', description='Список зарегистрированных персонажей сервера')
        @app_commands.guild_only()
        @app_commands.describe(page='Номер страницы, по 10 персонажей')
        async def roster(inter: discord.Interaction, page: app_commands.Range[int,1,10000] = 1):
            rows = self.store.roster(inter.guild_id)
            pages = max(1, (len(rows)+9)//10)
            if page > pages:
                await inter.response.send_message(f'Всего страниц: {pages}.', ephemeral=True)
                return
            lines = [f'**{escape(d["name"])}** — {escape(d["species"])}, {escape(d["class"])} (<@{uid}>)' for uid,d in rows[(page-1)*10:page*10]]
            embed = discord.Embed(title='Гильдия фурсон', description='\n'.join(lines) or 'Пока пусто.', colour=0x9B7AD1)
            embed.set_footer(text=f'Страница {page}/{pages} · персонажей: {len(rows)}')
            await inter.response.send_message(embed=embed)

        @tree.command(name='forget', description='Удалить свою фурсону и аватар из базы бота')
        @app_commands.guild_only()
        @app_commands.describe(confirm='Подтверди удаление')
        async def forget(inter: discord.Interaction, confirm: bool):
            if not confirm:
                await inter.response.send_message('Удаление отменено.', ephemeral=True)
                return
            key = (inter.guild_id, inter.user.id)
            if key in self.active:
                await inter.response.send_message('Сначала дождись завершения текущей генерации.', ephemeral=True)
                return
            deleted = self.store.delete(*key)
            await inter.response.send_message('Фурсона удалена из базы. Старые сообщения Discord не удаляются.' if deleted else 'Фурсоны нет.', ephemeral=True)

        @tree.error
        async def on_command_error(inter: discord.Interaction, error: app_commands.AppCommandError):
            LOG.error('Command error: %s', type(error).__name__)
            if inter.response.is_done():
                await inter.followup.send('Ошибка команды. Проверь журнал бота.', ephemeral=True)
            else:
                await inter.response.send_message('Ошибка команды. Проверь журнал бота.', ephemeral=True)


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    for key in ('DISCORD_TOKEN', 'DISCORD_GUILD_ID', 'OPENAI_API_KEY'):
        if not os.getenv(key):
            raise SystemExit(f'Заполни {key} в .env')
    if os.getenv('IMAGE_QUALITY','medium') not in ('low','medium','high','auto'):
        raise SystemExit('IMAGE_QUALITY: low, medium, high или auto')
    FurBot().run(os.environ['DISCORD_TOKEN'], log_handler=None)

if __name__ == '__main__':
    main()
