import asyncio
import base64
import copy
import json
import unittest
from unittest.mock import AsyncMock
from engine import CloudGenerator, GenerationError, SYSTEM, extract_text, validate, avatar_filename

EXAMPLE = dict(name='Мира', species='Снежный барс', background='Картограф', personality='Любопытна', backstory='Мира рисует карты горных троп.', trait_name='Горная память', trait='Помнит старые тропы.', appearance='Gray fur and blue scarf', **{'class':'Следопыт'}, alignment='Истинно нейтральный', stats=dict(str=10,dex=15,con=13,int=12,wis=14,cha=8))

def response(data):
    return {'success':True,'result':{'response':data}}

class EngineTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate(copy.deepcopy(EXAMPLE)), EXAMPLE)
    def test_invalid_stats(self):
        for val in (16, True, 9):
            data = copy.deepcopy(EXAMPLE)
            data['stats']['dex'] = val
            with self.assertRaises(GenerationError):
                validate(data)
    def test_wrong_class(self):
        data=copy.deepcopy(EXAMPLE); data['class']='Не класс'
        with self.assertRaises(GenerationError): validate(data)
    def test_refusal(self):
        with self.assertRaises(GenerationError):
            extract_text({'success':False,'result':None})
    def test_cloud_repair(self):
        gen=CloudGenerator(None,'account','key','text','image',4)
        bad=copy.deepcopy(EXAMPLE); bad['stats']['dex']=16
        gen.post=AsyncMock(side_effect=[response(bad),response(EXAMPLE)])
        self.assertEqual(asyncio.run(gen.character([])),EXAMPLE)
        self.assertEqual(gen.post.await_count,2)
        self.assertEqual(gen.post.await_args.args[1]['messages'][0]['content'], SYSTEM)
        self.assertIn('Абсурд должен быть связным', SYSTEM)
    def test_portrait(self):
        gen=CloudGenerator(None,'account','key','text','image',4)
        avatar=b'RIFF'+b'1234'+b'WEBP'+b'test'
        gen.post=AsyncMock(return_value={'success':True,'result':{'image':base64.b64encode(avatar).decode()}})
        self.assertEqual(asyncio.run(gen.portrait(EXAMPLE)),avatar)
        self.assertEqual(gen.post.await_args.args[0], 'image')
        prompt = gen.post.await_args.args[1]['prompt']
        self.assertIn('furry OC profile icon', prompt)
        self.assertIn('upright anthropomorphic animal PERSON', prompt)
        self.assertIn('waist-up close portrait', prompt)
        self.assertIn('One absurd accessory only', prompt)
        self.assertIn('no wildlife photography', prompt)
        self.assertIn(EXAMPLE['appearance'], prompt)
        self.assertLessEqual(len(prompt), 2048)

    def test_surreal_story_prompt(self):
        self.assertIn('Минимум три разных сюрреалистических поворота', SYSTEM)
        self.assertIn('Каждое предложение', SYSTEM)
        self.assertIn('Anthropomorphic', SYSTEM)
    def test_invalid_portrait(self):
        gen=CloudGenerator(None,'account','key','text','image',4)
        gen.post=AsyncMock(return_value={'success':True,'result':{'image':'abcd'}})
        with self.assertRaises(GenerationError): asyncio.run(gen.portrait(EXAMPLE))

    def test_json_string(self):
        self.assertEqual(extract_text(response(json.dumps(EXAMPLE))),EXAMPLE)
    def test_image_formats(self):
        self.assertEqual(avatar_filename(b'\xff\xd8\xff'+b'x'*20),'fursona.jpg')
        self.assertEqual(avatar_filename(b'\x89PNG\r\n\x1a\n'+b'x'*20),'fursona.png')
        self.assertEqual(avatar_filename(b'RIFF1234WEBP'),'fursona.webp')

    def test_repetitions_trigger_repair(self):
        gen=CloudGenerator(None,'account','key','text','image',4)
        bad=copy.deepcopy(EXAMPLE); bad['backstory']='Лестница, лестницы и лестницей. Потом шкаф.'
        gen.post=AsyncMock(side_effect=[response(bad),response(EXAMPLE)])
        self.assertEqual(asyncio.run(gen.character([])),EXAMPLE)
        self.assertEqual(gen.post.await_count,2)

    def test_recent_overlap(self):
        from style import repetition_issue
        data=dict(EXAMPLE,backstory='Сырники холодные куплены возле вокзала. Сырники понравились.')
        recent=[dict(data, name='А'),dict(data,name='Б')]
        self.assertTrue(repetition_issue(data,recent))

    def test_style_cannot_block_valid_card(self):
        gen=CloudGenerator(None,'account','key','text','image',4)
        bad_style=dict(EXAMPLE,backstory='Лестница, лестницы и лестницей. Потом шкаф.')
        gen.post=AsyncMock(side_effect=[response(bad_style),response(bad_style)])
        self.assertEqual(asyncio.run(gen.character([])),bad_style)
        self.assertEqual(gen.post.await_count,2)

    def test_style_retry_invalid_falls_back(self):
        gen=CloudGenerator(None,'account','key','text','image',4)
        candidate=dict(EXAMPLE,backstory='Лестница, лестницы и лестницей. Потом шкаф.')
        invalid=copy.deepcopy(EXAMPLE); invalid['stats']['dex']=16
        gen.post=AsyncMock(side_effect=[response(candidate),response(invalid)])
        self.assertEqual(asyncio.run(gen.character([])),candidate)

    def test_style_retry_api_error_falls_back(self):
        gen=CloudGenerator(None,'account','key','text','image',4)
        candidate=dict(EXAMPLE,backstory='Лестница, лестницы и лестницей. Потом шкаф.')
        gen.post=AsyncMock(side_effect=[response(candidate),GenerationError('HTTP 429')])
        self.assertEqual(asyncio.run(gen.character([])),candidate)

    def test_long_valid_story_is_not_fatal(self):
        gen=CloudGenerator(None,'account','key','text','image',4)
        candidate=dict(EXAMPLE,backstory='А'*481)
        gen.post=AsyncMock(side_effect=[response(candidate),response(candidate)])
        self.assertEqual(asyncio.run(gen.character([])),candidate)

    def test_schema_failure_reports_reason(self):
        gen=CloudGenerator(None,'account','key','text','image',4)
        invalid=copy.deepcopy(EXAMPLE); invalid['stats']['dex']=14
        gen.post=AsyncMock(return_value=response(invalid))
        with self.assertRaisesRegex(GenerationError,'Point Buy: 25 вместо 27'):
            asyncio.run(gen.character([]))
        self.assertEqual(gen.post.await_count,3)

    def test_last_attempt_valid_despite_style(self):
        gen=CloudGenerator(None,'account','key','text','image',4)
        invalid=copy.deepcopy(EXAMPLE); invalid['stats']['dex']=16
        candidate=dict(EXAMPLE,backstory='Лестница, лестницы и лестницей. Потом шкаф.')
        gen.post=AsyncMock(side_effect=[response(invalid),response(invalid),response(candidate)])
        self.assertEqual(asyncio.run(gen.character([])),candidate)
