import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import discord
from bot import (FurBot, Panel, DraftView, PANEL_CREATE, PANEL_PROFILE,
                 draft_custom_id, route_component, edit_interaction_card)

REV='a'*32

def fake_bot():
    return SimpleNamespace(user=SimpleNamespace(id=900),
        open_panel=AsyncMock(),handle_draft_component=AsyncMock(),
        expired_component=AsyncMock())

def fake_inter(custom_id,author=900):
    return SimpleNamespace(type=discord.InteractionType.component,
        data={'custom_id':custom_id,'component_type':2},
        message=SimpleNamespace(author=SimpleNamespace(id=author),flags=SimpleNamespace(ephemeral=True)),
        guild_id=1,user=SimpleNamespace(id=2),id=100,
        created_at=discord.utils.utcnow(),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),edit_original_response=AsyncMock())

class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_after_restart(self):
        for cid,generate in [(PANEL_CREATE,True),(PANEL_PROFILE,False)]:
            bot=fake_bot(); inter=fake_inter(cid)
            await route_component(bot,inter)
            inter.response.defer.assert_awaited_once_with(ephemeral=True,thinking=True)
            bot.open_panel.assert_awaited_once_with(inter,generate=generate)

    async def test_draft_buttons_survive_restart(self):
        original=DraftView(fake_bot(),1,2,REV)
        self.assertTrue(original.is_finished())
        for item,action in zip(original.children,['r','a']):
            bot=fake_bot(); inter=fake_inter(item.custom_id)
            await route_component(bot,inter)
            bot.handle_draft_component.assert_awaited_once_with(inter,action,1,2,REV)
            inter.response.defer.assert_awaited_once_with(ephemeral=True,thinking=False)

    async def test_old_button_gets_recovery(self):
        bot=fake_bot(); inter=fake_inter('b'*32)
        await route_component(bot,inter)
        bot.expired_component.assert_awaited_once_with(inter)
        bot.handle_draft_component.assert_not_awaited()
        inter.response.defer.assert_awaited_once()

    async def test_other_application_ignored(self):
        bot=fake_bot(); inter=fake_inter(PANEL_CREATE,author=901)
        await route_component(bot,inter)
        inter.response.defer.assert_not_awaited()
        bot.open_panel.assert_not_awaited()

    async def test_owner_and_guild_checked_before_mutation(self):
        bot=SimpleNamespace(is_panel_channel=lambda inter:True,
            generate_draft=AsyncMock(),accept_draft=AsyncMock())
        inter=fake_inter(draft_custom_id('a',1,2,REV))
        await FurBot.handle_draft_component(bot,inter,'a',1,3,REV)
        await FurBot.handle_draft_component(bot,inter,'a',3,2,REV)
        bot.accept_draft.assert_not_awaited()
        self.assertEqual(inter.followup.send.await_count,2)

    async def test_revision_passed_unchanged(self):
        bot=SimpleNamespace(is_panel_channel=lambda inter:True,
            generate_draft=AsyncMock(),accept_draft=AsyncMock())
        inter=fake_inter(draft_custom_id('a',1,2,REV))
        await FurBot.handle_draft_component(bot,inter,'a',1,2,REV)
        bot.accept_draft.assert_awaited_once_with(inter,REV)
        await FurBot.handle_draft_component(bot,inter,'r',1,2,REV)
        bot.generate_draft.assert_awaited_once_with(inter,expected=REV)

    async def test_ack_failure_does_not_generate(self):
        bot=fake_bot(); inter=fake_inter(PANEL_CREATE)
        inter.response.defer.side_effect=TimeoutError()
        await route_component(bot,inter)
        bot.open_panel.assert_not_awaited()
        inter.followup.send.assert_not_awaited()

    async def test_id_length_and_components(self):
        value=draft_custom_id('a',2**64-1,2**64-1,REV)
        self.assertLessEqual(len(value),100)
        panel=Panel(fake_bot())
        self.assertTrue(panel.is_finished())
        self.assertEqual([x.custom_id for x in panel.children],[PANEL_CREATE,PANEL_PROFILE])
        self.assertEqual(len(panel.to_components()[0]['components']),2)

    async def test_wrong_owner_does_not_update_source(self):
        bot=fake_bot(); inter=fake_inter(draft_custom_id('a',1,3,REV))
        await route_component(bot,inter)
        inter.response.defer.assert_awaited_once_with(ephemeral=True,thinking=True)

    async def test_public_draft_button_not_routed_to_mutation(self):
        bot=fake_bot(); inter=fake_inter(draft_custom_id('a',1,2,REV))
        inter.message.flags.ephemeral=False
        await route_component(bot,inter)
        bot.expired_component.assert_awaited_once()
        bot.handle_draft_component.assert_not_awaited()

    async def test_card_edit_replaces_attachment_without_followup(self):
        from test_engine import EXAMPLE
        inter=fake_inter(PANEL_CREATE)
        member=SimpleNamespace(display_name='Member')
        view=DraftView(fake_bot(),1,2,REV)
        avatar=b'RIFF1234WEBPtest'
        await edit_interaction_card(inter,EXAMPLE,avatar,member,view=view)
        inter.edit_original_response.assert_awaited_once()
        kwargs=inter.edit_original_response.await_args.kwargs
        self.assertEqual(len(kwargs['attachments']),1)
        self.assertEqual(kwargs['attachments'][0].filename,'fursona.webp')
        self.assertIs(kwargs['view'],view)
        self.assertIn(EXAMPLE['name'],kwargs['content'])
        inter.followup.send.assert_not_awaited()

    async def test_accept_edit_removes_buttons(self):
        from test_engine import EXAMPLE
        inter=fake_inter(PANEL_CREATE)
        await edit_interaction_card(inter,EXAMPLE,b'RIFF1234WEBPtest',
                                    SimpleNamespace(display_name='Member'),accepted=True)
        self.assertIsNone(inter.edit_original_response.await_args.kwargs['view'])
        self.assertIn('Принятая фурсона',inter.edit_original_response.await_args.kwargs['content'])
