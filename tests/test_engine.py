import asyncio
import base64
import copy
import json
import unittest
from unittest.mock import AsyncMock
from engine import CloudGenerator, GenerationError, SYSTEM, extract_text, validate

EXAMPLE = dict(name='Мира', species='Снежный барс', background='Картограф', personality='Любопытна', backstory='Мира рисует карты горных троп.', trait_name='Горная память', trait='Помнит старые тропы.', appearance='Gray fur and blue scarf', **{'class':'Следопыт'}, alignment='Истинно нейтральный', stats=dict(str=10,dex=15,con=13,int=12,wis=14,cha=8))

def response(data):
    return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(data)}]}]}

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
            extract_text({'status':'completed','output':[{'type':'message','content':[{'type':'refusal'}]}]})
    def test_cloud_repair(self):
        gen=CloudGenerator(None,'key','text','image','medium')
        bad=copy.deepcopy(EXAMPLE); bad['stats']['dex']=16
        gen.post=AsyncMock(side_effect=[response(bad),response(EXAMPLE)])
        self.assertEqual(asyncio.run(gen.character([])),EXAMPLE)
        self.assertEqual(gen.post.await_count,2)
        self.assertEqual(gen.post.await_args.args[1]['instructions'], SYSTEM)
        self.assertIn('Абсурд должен быть связным', SYSTEM)
    def test_portrait(self):
        gen=CloudGenerator(None,'key','text','image','medium')
        avatar=b'RIFF'+b'1234'+b'WEBP'+b'test'
        gen.post=AsyncMock(return_value={'data':[{'b64_json':base64.b64encode(avatar).decode()}]})
        self.assertEqual(asyncio.run(gen.portrait(EXAMPLE)),avatar)
        self.assertEqual(gen.post.await_args.args[0], 'images/generations')
        self.assertIn('deadpan character comedy', gen.post.await_args.args[1]['prompt'])
    def test_invalid_portrait(self):
        gen=CloudGenerator(None,'key','text','image','medium')
        gen.post=AsyncMock(return_value={'data':[{'b64_json':'abcd'}]})
        with self.assertRaises(GenerationError): asyncio.run(gen.portrait(EXAMPLE))
