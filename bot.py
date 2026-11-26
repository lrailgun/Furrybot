"""Discord prefix commands and private drafts with permanent acceptance."""
from __future__ import annotations
import asyncio
import io
import logging
import re
import time
from collections import Counter
import aiohttp
import discord
from discord import app_commands
from config import runtime_config
from engine import CloudGenerator, GenerationError, STATS, avatar_filename
from storage import Store

LOG = logging.getLogger('furrybot')
LABELS = dict(zip(STATS, ('СИЛ','ЛОВ','ТЕЛ','ИНТ','МДР','ХАР')))
PREFIX = '!furry'
ADMIN_COLOUR = 0xFFB668
MEMBER_COLOUR = 0xE74C3C
PREFERRED_ADMIN_ID = 221764587060461569
GENERATION_STATUS = 'Awwtysm Furry Bot определяет вашу судьбу. Пожалуйста, подождите...'
GENERATION_TIMEOUT = 300


def esc(value):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))


def card(data, avatar, member, accepted=False):
    stats=[]
    for k in STATS:
        v=data['stats'][k]
        stats.append(f'{LABELS[k]} {v:>2} ({(v-10)//2:+d})')
    lines=[
        f'## {esc(data["name"])}',
        f'**{esc(data["species"])} · {esc(data["class"])} · уровень 1**',
        f'{esc(data["alignment"])} · {esc(member.display_name)[:80]}',
        '',
        '```',
        '  '.join(stats[:3]),
        '  '.join(stats[3:]),
        '```',
        f'**Происхождение:** {esc(data["background"])}',
        f'**Характер:** {esc(data["personality"])}',
        '',
        '**Предыстория**',
        esc(data["backstory"]),
        '',
        f'**{esc(data["trait_name"])}:** {esc(data["trait"])}',
        '',
        '-# '+('Принятая фурсона' if accepted else 'Черновик · можно перегенерировать')+' · Point Buy 27',
    ]
    return '\n'.join(lines), discord.File(io.BytesIO(avatar),filename=avatar_filename(avatar))


async def send_card(send, data, avatar, member, accepted=False, note=None, **kwargs):
    # Separate attachment and plain message: no narrow embed column.
    body, file = card(data, avatar, member, accepted=accepted)
    if note:
        body = note + '\n\n' + body
    if len(body) > 2000:
        body = body[:1999] + '…'
    visibility = {'ephemeral': kwargs['ephemeral']} if 'ephemeral' in kwargs else {}
    await send(file=file, **visibility)
    await send(content=body, **kwargs)


async def edit_interaction_card(inter, data, avatar, member, accepted=False, note=None, view=None):
    """Replace the original private interaction message, including its attachment."""
    body,file=card(data,avatar,member,accepted=accepted)
    if note:
        body=note+'\n\n'+body
    if len(body)>2000:
        body=body[:1999]+'…'
    try:
        await inter.edit_original_response(content=body,attachments=[file],view=view,
                                           allowed_mentions=discord.AllowedMentions.none())
    finally:
        file.close()


async def generation_progress(inter, replacing):
    # A separate, visible private followup for rerolls, while the old card remains intact.
    if replacing:
        message=await inter.followup.send(GENERATION_STATUS,ephemeral=True,wait=True)
        LOG.info('Generation progress sent: interaction=%s',inter.id)
        return message
    await inter.edit_original_response(content=GENERATION_STATUS,allowed_mentions=discord.AllowedMentions.none())
    LOG.info('Generation progress displayed: interaction=%s',inter.id)
    return None


async def generation_failure(inter, replacing, message):
    if replacing:
        await inter.followup.send(message,ephemeral=True)
    else:
        await inter.edit_original_response(content=message,allowed_mentions=discord.AllowedMentions.none())


async def acknowledge_and_run(inter, handler, *args, update_message=False, **kwargs):
    started=time.monotonic()
    age=(discord.utils.utcnow()-inter.created_at).total_seconds()
    LOG.info('Interaction dispatch: id=%s event_age_ms=%.0f',inter.id,age*1000)
    try:
        await inter.response.defer(ephemeral=True, thinking=not update_message)
    except discord.NotFound as exc:
        if exc.code == 10062:
            LOG.warning('Interaction expired before ACK: id=%s event_age_ms=%.0f ack_elapsed_ms=%.0f',
                        inter.id,age*1000,(time.monotonic()-started)*1000)
            return
        LOG.exception('Interaction ACK failed: id=%s',inter.id)
        return
    except Exception:
        LOG.exception('Interaction ACK failed: id=%s elapsed_ms=%.0f',
                      inter.id,(time.monotonic()-started)*1000)
        return
    LOG.info('Interaction acknowledged: guild=%s id=%s elapsed_ms=%.0f',
             inter.guild_id,inter.id,(time.monotonic()-started)*1000)
    try:
        await handler(inter, *args, **kwargs)
    except Exception:
        LOG.exception('Interaction handler failed: guild=%s id=%s', inter.guild_id, inter.id)
        try:
            await inter.followup.send('Внутренняя ошибка. Проверь журнал Furrybot.', ephemeral=True)
        except discord.HTTPException:
            LOG.warning('Не удалось ответить на interaction=%s', inter.id)


DRAFT_ID_RE = re.compile(r'furrybot:d:(r|a):([0-9]{1,20}):([0-9]{1,20}):([a-f0-9]{32})')
LEGACY_DRAFT_RE = re.compile(r'[a-f0-9]{32}')
PANEL_CREATE = 'furrybot:panel:create:v2'
PANEL_PROFILE = 'furrybot:panel:profile:v2'


def draft_custom_id(action,guild,user,revision):
    value=f'furrybot:d:{action}:{guild}:{user}:{revision}'
    if not DRAFT_ID_RE.fullmatch(value) or len(value)>100:
        raise ValueError('Invalid draft component ID')
    return value


class Panel(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label='Создать новую',style=discord.ButtonStyle.primary,custom_id=PANEL_CREATE))
        self.add_item(discord.ui.Button(label='Моя фурсона',style=discord.ButtonStyle.secondary,custom_id=PANEL_PROFILE))
        # Components are serialized, but all dispatch is handled by on_interaction.
        self.stop()


class DraftView(discord.ui.View):
    def __init__(self, bot, guild, user, revision):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label='Ещё вариант',style=discord.ButtonStyle.secondary,
                                        custom_id=draft_custom_id('r',guild,user,revision)))
        self.add_item(discord.ui.Button(label='Принять навсегда',style=discord.ButtonStyle.success,
                                        custom_id=draft_custom_id('a',guild,user,revision)))
        self.stop()


async def route_component(bot, inter):
    """Stateless dispatch; no per-message View callback state is required."""
    if inter.type != discord.InteractionType.component:
        return
    data=inter.data or {}
    custom_id=data.get('custom_id','')
    if not isinstance(custom_id,str):
        return
    if data.get('component_type') != discord.ComponentType.button.value:
        return
    # Only route components on messages sent by this application.
    if not bot.user or not inter.message or inter.message.author.id != bot.user.id:
        return
    LOG.info('Interaction received: guild=%s id=%s custom_id=%s',inter.guild_id,inter.id,custom_id[:100])
    if custom_id in (PANEL_CREATE,PANEL_PROFILE):
        await acknowledge_and_run(inter,bot.open_panel,generate=custom_id==PANEL_CREATE)
        return
    match=DRAFT_ID_RE.fullmatch(custom_id)
    if match:
        action,guild,user,revision=match.groups()
        # Updating the source is safe only for its owner and a private bot message.
        private=bool(getattr(getattr(inter.message,'flags',None),'ephemeral',False))
        if not private:
            await acknowledge_and_run(inter,bot.expired_component)
            return
        same_owner=inter.guild_id==int(guild) and inter.user.id==int(user)
        await acknowledge_and_run(inter,bot.handle_draft_component,action,int(guild),int(user),revision,
                                  update_message=same_owner)
        return
    if LEGACY_DRAFT_RE.fullmatch(custom_id) or custom_id.startswith('furrybot:'):
        await acknowledge_and_run(inter,bot.expired_component)


class FurBot(discord.Client):
    def __init__(self,cfg):
        intents=discord.Intents.default()
        intents.message_content=True
        super().__init__(intents=intents,allowed_mentions=discord.AllowedMentions.none())
        self.cfg=cfg
        self.max_parallel=int(cfg['MAX_PARALLEL'])
        self.store=Store(cfg['DATABASE_PATH'])
        self.busy=set()
        self.install_lock=asyncio.Lock()
        self.session=None
        self.cloud=None

    async def setup_hook(self):
        self.session=aiohttp.ClientSession()
        self.cloud=CloudGenerator(self.session,self.cfg['CLOUDFLARE_ACCOUNT_ID'],self.cfg['CLOUDFLARE_API_TOKEN'],self.cfg['TEXT_MODEL'],self.cfg['IMAGE_MODEL'],int(self.cfg['IMAGE_STEPS']))
        self.legacy_tree=app_commands.CommandTree(self)

    async def clear_legacy_commands(self,guild):
        # Only clear this application's legacy guild commands after authorized installation.
        try:
            if await self.legacy_tree.fetch_commands(guild=guild):
                await self.legacy_tree.sync(guild=guild)
        except discord.HTTPException:
            LOG.warning('Не удалось удалить устаревшие slash-команды для guild=%s',guild.id)

    async def on_ready(self):
        LOG.info('Подключён как %s',self.user)

    async def on_interaction(self, inter):
        await route_component(self,inter)

    async def expired_component(self,inter):
        await inter.followup.send('Эта кнопка относится к старой карточке. Открой «Создать новую» в канале фуррификации.',ephemeral=True)

    async def handle_draft_component(self,inter,action,guild,user,revision):
        if inter.guild_id != guild or inter.user.id != user or not self.is_panel_channel(inter):
            await inter.followup.send('Эта карточка не для тебя или панель устарела.',ephemeral=True)
            return
        if action == 'r':
            await self.generate_draft(inter,expected=revision)
        elif action == 'a':
            await self.accept_draft(inter,revision)

    async def close(self):
        try:
            if self.session and not self.session.closed:
                await self.session.close()
            await super().close()
        finally:
            self.store.close()

    def is_panel_channel(self,inter):
        if inter.guild_id is None:
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
        await edit_interaction_card(inter,data,avatar,inter.user,note='Это черновик. Принятие необратимо для обычного участника.',view=DraftView(self,inter.guild_id,inter.user.id,revision))

    async def open_panel(self,inter,generate):
        if not self.is_panel_channel(inter):
            await inter.followup.send('Панель устарела.',ephemeral=True)
            return
        key=(inter.guild_id,inter.user.id)
        final=self.store.get(*key)
        if final:
            note=await self.grant_role(inter.user)
            await edit_interaction_card(inter,*final,inter.user,accepted=True,note=note or 'Твоя принятая фурсона.')
        elif generate:
            # The create button always requests a fresh draft, even after restart.
            await self.generate_draft(inter)
        elif (draft:=self.store.draft(*key)):
            await self.show_draft(inter,draft)
        else:
            await inter.followup.send('Фурсоны пока нет. Нажми «Создать новую».',ephemeral=True)

    async def generate_draft(self,inter,expected=None):
        key=(inter.guild_id,inter.user.id)
        current=self.store.draft(*key)
        if not self.is_panel_channel(inter) or self.store.get(*key):
            await inter.followup.send('Перегенерация недоступна: фурсона уже принята или панель устарела.',ephemeral=True)
            return
        if expected is not None and (not current or current[0]!=expected):
            await inter.followup.send('Этот вариант устарел. Открой актуальный черновик через панель.',ephemeral=True)
            return
        if key in self.busy or len(self.busy)>=self.max_parallel:
            await inter.followup.send('Генерация уже идёт или все слоты заняты. Попробуй позже.',ephemeral=True)
            return
        self.busy.add(key)
        started=time.monotonic()
        replacing=expected is not None
        progress=None
        saved=False
        try:
            progress=await generation_progress(inter,replacing=replacing)
            LOG.info('Generation started: guild=%s user=%s',*key)
            async with asyncio.timeout(GENERATION_TIMEOUT):
                LOG.info('Generation stage: text guild=%s user=%s',*key)
                data=await self.cloud.character(self.store.recent(key[0]))
                LOG.info('Generation stage: avatar guild=%s user=%s elapsed_s=%.1f',*key,time.monotonic()-started)
                avatar=await self.cloud.portrait(data)
            revision=self.store.save_draft(*key,data,avatar)
            if not revision:
                await generation_failure(inter,replacing,'Фурсона уже принята. Черновик не сохранён.')
                return
            saved=True
            LOG.info('Generation stage: delivery guild=%s user=%s elapsed_s=%.1f',*key,time.monotonic()-started)
            await self.show_draft(inter,(revision,data,avatar))
            LOG.info('Generation finished: guild=%s user=%s elapsed_s=%.1f',*key,time.monotonic()-started)
        except TimeoutError:
            LOG.warning('Generation deadline exceeded: guild=%s user=%s elapsed_s=%.1f',*key,time.monotonic()-started)
            await generation_failure(inter,replacing,'Генерация остановлена: превышено время ожидания. Старый черновик, если был, сохранён.')
        except GenerationError as exc:
            LOG.warning('Generation failed: guild=%s user=%s reason=%s',*key,exc)
            await generation_failure(inter,replacing,f'Генерация не завершена: {exc}. Старый черновик, если был, сохранён.')
        except Exception:
            LOG.exception('Ошибка генерации')
            message=('Новый черновик сохранён, но не удалось показать карточку. Открой «Создать новую».'
                     if saved else 'Внутренняя ошибка. Открой панель повторно.')
            try:
                await generation_failure(inter,replacing,message)
            except discord.HTTPException:
                LOG.warning('Не удалось обновить сообщение генерации: id=%s',inter.id)
        finally:
            self.busy.discard(key)
            if progress is not None:
                try:
                    await progress.delete()
                except discord.HTTPException:
                    LOG.warning('Could not delete generation status: interaction=%s',inter.id)

    async def accept_draft(self,inter,revision):
        key=(inter.guild_id,inter.user.id)
        if not self.is_panel_channel(inter) or key in self.busy:
            await inter.followup.send('Сейчас нельзя принять фурсону: генерация идёт или панель устарела.',ephemeral=True)
            return
        # No await between checking and atomic acceptance. Old buttons cannot accept new drafts.
        if not self.store.accept(*key,revision):
            await inter.followup.send('Фурсона уже принята или этот черновик устарел.',ephemeral=True)
            return
        self.busy.add(key)
        try:
            note=await self.grant_role(inter.user)
            await edit_interaction_card(inter,*self.store.get(*key),inter.user,accepted=True,note=note or 'Фурсона принята! Роль выдана.')
        finally:
            self.busy.discard(key)

    async def grant_preferred_admin(self, message, role, explicit=False):
        # Grant only when the target is installing on their own behalf, or
        # a guild administrator explicitly requests the documented opt-in.
        if not explicit and message.author.id != PREFERRED_ADMIN_ID:
            return False
        guild = message.guild
        try:
            target = await guild.fetch_member(PREFERRED_ADMIN_ID)
        except discord.NotFound:
            return False
        except discord.HTTPException:
            LOG.exception('Preferred admin lookup failed: guild=%s', guild.id)
            return False
        if not target.guild_permissions.administrator:
            return False
        if role in target.roles:
            return True
        if role.managed or role.is_default() or role.permissions.value != 0 or not guild.me or role >= guild.me.top_role:
            LOG.warning('Preferred admin role is not safe to grant: guild=%s role=%s', guild.id, role.id)
            return False
        try:
            await target.add_roles(role, reason=f'Furrybot installation authorized by {message.author.id}')
            LOG.info('Furrybot admin role granted: guild=%s actor=%s target=%s role=%s', guild.id, message.author.id, target.id, role.id)
            return True
        except discord.HTTPException:
            LOG.exception('Preferred admin role grant failed: guild=%s', guild.id)
            return False

    async def install_panel(self,message, arg=''):
        guild=message.guild
        if not message.author.guild_permissions.administrator:
            await message.reply('Установка доступна только администраторам Discord.')
            return
        if arg not in ('', '--grant-admin'):
            await message.reply('Использование: `!furry install` или `!furry install --grant-admin` (назначить настроенного администратора бота).')
            return
        async with self.install_lock:
            old=self.store.installation(guild.id) or {}
            me=guild.me
            if not me or not me.guild_permissions.manage_roles or not me.guild_permissions.manage_channels:
                await message.reply('Боту нужны права Manage Roles и Manage Channels.')
                return
            async def resolve_role(key, name, colour):
                role=guild.get_role(old.get(key,0))
                if not role:
                    matches=[r for r in guild.roles if r.name==name and not r.managed and not r.is_default()]
                    if len(matches)>1:
                        raise ValueError('Найдено несколько ролей '+name+'; оставь одну.')
                    role=matches[0] if matches else await guild.create_role(name=name,permissions=discord.Permissions.none(),colour=discord.Colour(colour),reason='Furrybot setup')
                if role.colour.value != colour:
                    if role.managed or role.is_default() or role >= me.top_role:
                        raise ValueError('Не могу изменить цвет роли '+name+': проверь иерархию ролей.')
                    role=await role.edit(colour=discord.Colour(colour),reason='Furrybot setup: role colour')
                return role
            try:
                admin=await resolve_role('admin_role',self.cfg['ADMIN_ROLE'],ADMIN_COLOUR)
                member=await resolve_role('member_role',self.cfg['MEMBER_ROLE'],MEMBER_COLOUR)
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
                await self.clear_legacy_commands(guild)
                granted=await self.grant_preferred_admin(message,admin,explicit=arg=='--grant-admin')
                reply=f'Панель готова: {channel.mention}.'
                if not granted:
                    reply+=f' Для смены принятых фурсон администратору нужна роль «{esc(admin.name)}».'
                await message.reply(reply)
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
            await send_card(message.channel.send,data,avatar,member,accepted=True)
            LOG.info('admin replacement: guild=%s actor=%s target=%s',key[0],actor.id,key[1])
        except GenerationError as exc:
            await message.reply(f'Ошибка генерации: {exc}. Прежний персонаж сохранён.')
        finally:
            self.busy.discard(key)

    async def clean_channel(self,message):
        if not message.author.guild_permissions.administrator:
            await message.reply('Очистка доступна только администраторам Discord.')
            return
        async with self.install_lock:
            setup=self.store.installation(message.guild.id)
            channel=message.guild.get_channel(setup['channel']) if setup else None
            if not isinstance(channel,discord.TextChannel) or not setup.get('panel'):
                await message.reply('Канал или панель не найдены. Сначала выполни !furry install.')
                return
            # Verify the stored panel exists before deleting anything.
            panel=await channel.fetch_message(setup['panel'])
            if panel.author.id!=self.user.id:
                await message.reply('Не удалось проверить панель. Очистка отменена.')
                return
            deleted=0
            failed=0
            async for item in channel.history(limit=None):
                if item.id==panel.id or item.author.id!=self.user.id or item.pinned:
                    continue
                try:
                    await item.delete()
                    deleted+=1
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    failed+=1
            LOG.info('Channel cleanup: guild=%s actor=%s channel=%s deleted=%s failed=%s',
                     message.guild.id,message.author.id,channel.id,deleted,failed)
            await message.reply(f'Удалено сообщений бота: {deleted}. Ошибок: {failed}. Панель, закреплённые сообщения и база фурсон сохранены.',
                                delete_after=15 if message.channel.id==channel.id else None)

    async def on_message(self,message):
        if message.author.bot or not message.guild:
            return
        parts=message.content.strip().split(maxsplit=2)
        if not parts or parts[0].casefold()!=PREFIX:
            return
        cmd=parts[1].casefold() if len(parts)>1 else 'help'
        arg=parts[2] if len(parts)>2 else ''
        try:
            if cmd=='install':
                await self.install_panel(message,arg)
            elif cmd=='clean':
                if arg:
                    await message.reply('Использование: !furry clean')
                    return
                await self.clean_channel(message)
            elif cmd in ('help','generate','reroll','accept'):
                setup=self.store.installation(message.guild.id)
                panel=f'<#{setup["channel"]}>' if setup else 'Панель ещё не установлена.'
                await message.reply(f'Панель: {panel}\n`!furry profile [@участник]` · `!furry avatar [@участник]` · `!furry roster` · `!furry species`\nАдмин: `!furry install`, `!furry regenerate @участник`, `!furry clean`. Генерация и принятие — кнопками панели.')
            elif cmd in ('profile','fursona','avatar'):
                member=await self.target(message,arg)
                final=self.store.get(message.guild.id,member.id)
                if not final:
                    await message.reply('У этого участника нет принятой фурсоны.')
                elif cmd=='avatar':
                    await message.reply(file=discord.File(io.BytesIO(final[1]),filename=avatar_filename(final[1])))
                else:
                    note=await self.grant_role(member) if member.id==message.author.id else ''
                    await send_card(message.reply,*final,member,accepted=True,note=note or None)
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
