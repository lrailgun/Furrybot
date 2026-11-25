"""Discord prefix commands and private drafts with permanent acceptance."""
from __future__ import annotations
import asyncio
import io
import logging
import re
from collections import Counter
import aiohttp
import discord
from discord import app_commands
from config import runtime_config
from engine import CloudGenerator, GenerationError, STATS
from storage import Store

LOG = logging.getLogger('furrybot')
LABELS = dict(zip(STATS, ('СИЛ','ЛОВ','ТЕЛ','ИНТ','МДР','ХАР')))
PREFIX = '!furry'


def esc(value):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))


def card(data, avatar, member, accepted=False):
    embed=discord.Embed(title=esc(data['name'])[:256], description=f"**{esc(data['species'])} · {esc(data['class'])} · уровень 1**\n{esc(data['alignment'])}", colour=0x9B7AD1)
    embed.set_author(name=member.display_name[:256])
    embed.set_thumbnail(url='attachment://fursona.webp')
    for k in STATS:
        v=data['stats'][k]
        embed.add_field(name=LABELS[k],value=f'**{v}** ({(v-10)//2:+d})',inline=True)
    for title,key in [('Происхождение','background'),('Характер','personality'),('Предыстория','backstory'),('Особенность: '+esc(data['trait_name']),'trait')]:
        embed.add_field(name=title[:256],value=esc(data[key])[:1024],inline=False)
    embed.set_footer(text=('Принятая фурсона' if accepted else 'Черновик · можно перегенерировать')+' · Point Buy 27')
    return {'embed':embed,'file':discord.File(io.BytesIO(avatar),filename='fursona.webp')}


class Panel(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot=bot

    @discord.ui.button(label='Создать / продолжить', style=discord.ButtonStyle.primary, custom_id='furrybot:panel:create:v2')
    async def create(self, inter, button):
        await self.bot.open_panel(inter, generate=True)

    @discord.ui.button(label='Моя фурсона', style=discord.ButtonStyle.secondary, custom_id='furrybot:panel:profile:v2')
    async def profile(self, inter, button):
        await self.bot.open_panel(inter, generate=False)


class DraftView(discord.ui.View):
    def __init__(self, bot, guild, user, revision):
        super().__init__(timeout=900)
        self.bot,self.guild,self.user,self.revision=bot,guild,user,revision

    async def interaction_check(self, inter):
        if inter.guild_id != self.guild or inter.user.id != self.user or not self.bot.is_panel_channel(inter):
            await inter.response.send_message('Эта карточка не для тебя или панель устарела.',ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Ещё вариант',style=discord.ButtonStyle.secondary)
    async def reroll(self, inter, button):
        await self.bot.generate_draft(inter, expected=self.revision)

    @discord.ui.button(label='Принять навсегда',style=discord.ButtonStyle.success)
    async def accept(self, inter, button):
        await self.bot.accept_draft(inter,self.revision)


class FurBot(discord.Client):
    def __init__(self,cfg):
        intents=discord.Intents.default()
        intents.message_content=True
        super().__init__(intents=intents,allowed_mentions=discord.AllowedMentions.none())
        self.cfg=cfg
        self.guild_id=int(cfg['DISCORD_GUILD_ID'])
        self.max_parallel=int(cfg['MAX_PARALLEL'])
        self.store=Store(cfg['DATABASE_PATH'])
        self.busy=set()
        self.install_lock=asyncio.Lock()
        self.session=None
        self.cloud=None

    async def setup_hook(self):
        self.session=aiohttp.ClientSession()
        self.cloud=CloudGenerator(self.session,self.cfg['OPENAI_API_KEY'],self.cfg['TEXT_MODEL'],self.cfg['IMAGE_MODEL'],self.cfg['IMAGE_QUALITY'])
        self.add_view(Panel(self))
        # Remove legacy v1 guild slash commands, not commands of other apps.
        try:
            tree=app_commands.CommandTree(self)
            guild=discord.Object(id=self.guild_id)
            if await tree.fetch_commands(guild=guild):
                await tree.sync(guild=guild)
        except discord.HTTPException:
            LOG.warning('Не удалось удалить устаревшие slash-команды')

    async def on_ready(self):
        LOG.info('Подключён как %s',self.user)

    async def close(self):
        try:
            if self.session and not self.session.closed:
                await self.session.close()
            await super().close()
        finally:
            self.store.close()

    def is_panel_channel(self,inter):
        if inter.guild_id!=self.guild_id:
            return False
        setup=self.store.installation(inter.guild_id)
        return bool(setup and setup['channel']==inter.channel_id)

    def is_admin(self,member):
        if not isinstance(member,discord.Member) or not member.guild_permissions.administrator:
            return False
        setup=self.store.installation(member.guild.id)
        return bool(setup and any(r.id==setup['admin_role'] for r in member.roles))

    async def grant_role(self,member):
        setup=self.store.installation(member.guild.id)
        role=member.guild.get_role(setup['member_role']) if setup else None
        if not role:
            return 'Роль не найдена. Администратору нужно повторить `!furry install`.'
        if role.managed or role.is_default() or not member.guild.me or role >= member.guild.me.top_role or role.permissions.value != 0:
            return 'Фурсона сохранена, но роль участника небезопасна для автоматической выдачи. Проверь её права и иерархию.'
        if role in member.roles:
            return ''
        try:
            await member.add_roles(role,reason='Фурсона принята')
            return ''
        except discord.HTTPException:
            LOG.warning('Не удалось выдать роль для guild=%s user=%s',member.guild.id,member.id)
            return 'Фурсона сохранена, но роль не выдана. Проверь права бота и порядок ролей; затем нажми «Моя фурсона» для повтора.'

    async def show_draft(self,inter,draft):
        revision,data,avatar=draft
        await inter.followup.send('Это черновик. Принятие необратимо для обычного участника.',**card(data,avatar,inter.user),view=DraftView(self,inter.guild_id,inter.user.id,revision),ephemeral=True)

    async def open_panel(self,inter,generate):
        if not self.is_panel_channel(inter):
            await inter.response.send_message('Панель устарела.',ephemeral=True)
            return
        key=(inter.guild_id,inter.user.id)
        final=self.store.get(*key)
        if final:
            await inter.response.defer(ephemeral=True,thinking=True)
            note=await self.grant_role(inter.user)
            await inter.followup.send(note or 'Твоя принятая фурсона.',**card(*final,inter.user,accepted=True),ephemeral=True)
        elif (draft:=self.store.draft(*key)):
            await inter.response.defer(ephemeral=True,thinking=True)
            await self.show_draft(inter,draft)
        elif generate:
            await self.generate_draft(inter)
        else:
            await inter.response.send_message('Фурсоны пока нет. Нажми «Создать / продолжить».',ephemeral=True)

    async def generate_draft(self,inter,expected=None):
        key=(inter.guild_id,inter.user.id)
        current=self.store.draft(*key)
        if not self.is_panel_channel(inter) or self.store.get(*key):
            await inter.response.send_message('Перегенерация недоступна: фурсона уже принята или панель устарела.',ephemeral=True)
            return
        if expected is not None and (not current or current[0]!=expected):
            await inter.response.send_message('Этот вариант устарел. Открой актуальный черновик через панель.',ephemeral=True)
            return
        if key in self.busy or len(self.busy)>=self.max_parallel:
            await inter.response.send_message('Генерация уже идёт или все слоты заняты. Попробуй позже.',ephemeral=True)
            return
        self.busy.add(key)
        try:
            await inter.response.defer(ephemeral=True,thinking=True)
            data=await self.cloud.character(self.store.recent(key[0]))
            avatar=await self.cloud.portrait(data)
            revision=self.store.save_draft(*key,data,avatar)
            if not revision:
                await inter.followup.send('Фурсона уже принята. Черновик не сохранён.',ephemeral=True)
                return
            await self.show_draft(inter,(revision,data,avatar))
        except GenerationError as exc:
            await inter.followup.send(f'Генерация не завершена: {exc}. Старый черновик сохранён.',ephemeral=True)
        except Exception:
            LOG.exception('Ошибка генерации')
            if inter.response.is_done():
                await inter.followup.send('Внутренняя ошибка. Открой панель повторно.',ephemeral=True)
        finally:
            self.busy.discard(key)

    async def accept_draft(self,inter,revision):
        key=(inter.guild_id,inter.user.id)
        if not self.is_panel_channel(inter) or key in self.busy:
            await inter.response.send_message('Сейчас нельзя принять фурсону: генерация идёт или панель устарела.',ephemeral=True)
            return
        # No await between checking and atomic acceptance. Old buttons cannot accept new drafts.
        if not self.store.accept(*key,revision):
            await inter.response.send_message('Фурсона уже принята или этот черновик устарел.',ephemeral=True)
            return
        self.busy.add(key)
        try:
            await inter.response.defer(ephemeral=True,thinking=True)
            note=await self.grant_role(inter.user)
            await inter.followup.send(note or 'Фурсона принята! Роль выдана.',**card(*self.store.get(*key),inter.user,accepted=True),ephemeral=True)
        finally:
            self.busy.discard(key)

    async def install_panel(self,message):
        guild=message.guild
        if not message.author.guild_permissions.administrator:
            await message.reply('Установка доступна только администраторам Discord.')
            return
        async with self.install_lock:
            old=self.store.installation(guild.id) or {}
            me=guild.me
            if not me or not me.guild_permissions.manage_roles or not me.guild_permissions.manage_channels:
                await message.reply('Боту нужны права Manage Roles и Manage Channels.')
                return
            async def resolve_role(key, name):
                role=guild.get_role(old.get(key,0))
                if role:
                    return role
                matches=[r for r in guild.roles if r.name==name and not r.managed and not r.is_default()]
                if len(matches)>1:
                    raise ValueError('Найдено несколько ролей '+name+'; оставь одну.')
                return matches[0] if matches else await guild.create_role(name=name,permissions=discord.Permissions.none(),reason='Furrybot setup')
            try:
                admin=await resolve_role('admin_role',self.cfg['ADMIN_ROLE'])
                member=await resolve_role('member_role',self.cfg['MEMBER_ROLE'])
                if admin.id==member.id or member>=me.top_role or member.managed or member.permissions.value!=0:
                    raise ValueError('Роль участника должна быть без прав, неуправляемой и ниже роли бота; роли должны отличаться.')
                channel=guild.get_channel(old.get('channel',0))
                if not isinstance(channel,discord.TextChannel):
                    channel=await guild.create_text_channel(self.cfg['CHANNEL_NAME'],overwrites={
                        guild.default_role:discord.PermissionOverwrite(view_channel=True,read_message_history=True,send_messages=False),
                        me:discord.PermissionOverwrite(view_channel=True,send_messages=True,embed_links=True,attach_files=True,read_message_history=True)},reason='Furrybot setup')
                self.store.set_installation(guild.id,channel.id,old.get('panel',0) if old.get('channel')==channel.id else 0,admin.id,member.id)
                embed=discord.Embed(title='Министерство принудительно-добровольной пушистости',description='Создай антропоморфное ходячее недоразумение. Перебирай варианты сколько угодно. Когда найдёшь своего персонажа, прими его: после этого сменить фурсону сможет только уполномоченный администратор.',colour=0x9B7AD1)
                panel=None
                if old.get('panel') and old.get('channel')==channel.id:
                    try:
                        panel=await channel.fetch_message(old['panel'])
                        if panel.author.id!=self.user.id:
                            panel=None
                    except discord.NotFound:
                        pass
                if panel:
                    await panel.edit(embed=embed,view=Panel(self))
                else:
                    panel=await channel.send(embed=embed,view=Panel(self))
                self.store.set_installation(guild.id,channel.id,panel.id,admin.id,member.id)
                await message.reply(f'Панель готова: {channel.mention}. Для смены принятых фурсон выдай нужным администраторам роль «{esc(admin.name)}».')
            except (discord.HTTPException,ValueError) as exc:
                LOG.warning('Ошибка установки панели: %s',type(exc).__name__)
                await message.reply(str(exc) if isinstance(exc,ValueError) else 'Не удалось установить панель. Проверь права и иерархию ролей.')

    async def target(self,message,arg):
        if not arg:
            return message.author
        match=re.fullmatch(r'(?:<@!?(\d+)>|(\d+))',arg.strip())
        if not match:
            raise ValueError('Укажи @участника или его ID.')
        uid=int(match.group(1) or match.group(2))
        return message.guild.get_member(uid) or await message.guild.fetch_member(uid)

    async def admin_regenerate(self,message,member):
        key=(message.guild.id,member.id)
        if not self.is_admin(message.author):
            await message.reply('Нужны права администратора Discord и роль «Его пушистое величество».')
            return
        if not self.store.get(*key):
            await message.reply('У участника пока нет принятой фурсоны.')
            return
        if key in self.busy or len(self.busy)>=self.max_parallel:
            await message.reply('Генерация уже идёт или все слоты заняты.')
            return
        self.busy.add(key)
        try:
            status=await message.reply('Начата административная перегенерация…')
            data=await self.cloud.character(self.store.recent(key[0]))
            avatar=await self.cloud.portrait(data)
            # Verify the caller's current permissions again after the long API call.
            actor=await message.guild.fetch_member(message.author.id)
            if not self.is_admin(actor) or not self.store.admin_replace(*key,data,avatar):
                await status.edit(content='Замена отменена: права или состояние изменились.')
                return
            note=await self.grant_role(member)
            await status.edit(content='Фурсона заменена администратором. '+note)
            await message.channel.send(**card(data,avatar,member,accepted=True))
            LOG.info('admin replacement: guild=%s actor=%s target=%s',key[0],actor.id,key[1])
        except GenerationError as exc:
            await message.reply(f'Ошибка генерации: {exc}. Прежний персонаж сохранён.')
        finally:
            self.busy.discard(key)

    async def on_message(self,message):
        if message.author.bot or not message.guild or message.guild.id!=self.guild_id:
            return
        parts=message.content.strip().split(maxsplit=2)
        if not parts or parts[0].casefold()!=PREFIX:
            return
        cmd=parts[1].casefold() if len(parts)>1 else 'help'
        arg=parts[2] if len(parts)>2 else ''
        try:
            if cmd=='install':
                await self.install_panel(message)
            elif cmd in ('help','generate','reroll','accept'):
                setup=self.store.installation(message.guild.id)
                panel=f'<#{setup["channel"]}>' if setup else 'Панель ещё не установлена.'
                await message.reply(f'Панель: {panel}\n`!furry profile [@участник]` · `!furry avatar [@участник]` · `!furry roster` · `!furry species`\nАдмин: `!furry install`, `!furry regenerate @участник`. Генерация и принятие — кнопками панели.')
            elif cmd in ('profile','fursona','avatar'):
                member=await self.target(message,arg)
                final=self.store.get(message.guild.id,member.id)
                if not final:
                    await message.reply('У этого участника нет принятой фурсоны.')
                elif cmd=='avatar':
                    await message.reply(file=discord.File(io.BytesIO(final[1]),filename='fursona.webp'))
                else:
                    note=await self.grant_role(member) if member.id==message.author.id else ''
                    await message.reply(note or None,**card(*final,member,accepted=True))
            elif cmd=='regenerate':
                if not arg:
                    await message.reply('Использование: `!furry regenerate @участник`')
                    return
                await self.admin_regenerate(message,await self.target(message,arg))
            elif cmd in ('roster','species'):
                rows=self.store.roster(message.guild.id)
                if cmd=='species':
                    counts=Counter(d['species'].casefold() for _,d in rows)
                    lines=[f'{esc(n.capitalize())} — {c}' for n,c in counts.most_common(25)]
                else:
                    lines=[f'{esc(d["name"])} — {esc(d["species"])}, {esc(d["class"])} (<@{uid}>)' for uid,d in rows[:20]]
                await message.reply(embed=discord.Embed(title='Принятые фурсоны' if cmd=='roster' else 'Виды',description=('\n'.join(lines) or 'Пока пусто.')[:4000]).set_footer(text=f'Всего персонажей: {len(rows)}'))
            else:
                await message.reply('Неизвестная команда. `!furry help`')
        except (discord.NotFound,ValueError):
            await message.reply('Участник не найден или некорректный аргумент.')
        except Exception:
            LOG.exception('Ошибка команды %s',cmd)
            try:
                await message.reply('Не удалось выполнить команду. Проверь журнал службы.')
            except discord.HTTPException:
                pass


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    cfg=runtime_config()
    FurBot(cfg).run(cfg['DISCORD_TOKEN'],log_handler=None)

if __name__=='__main__':
    main()
