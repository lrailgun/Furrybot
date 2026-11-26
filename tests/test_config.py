import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import config

class ConfigTests(unittest.TestCase):
    def test_no_guild_id_needed(self):
        secrets={'DISCORD_TOKEN':'example','CLOUDFLARE_ACCOUNT_ID':'a'*32,'CLOUDFLARE_API_TOKEN':'example'}
        with patch.dict(os.environ,secrets,clear=True),patch.object(config,'read_config',return_value=config.DEFAULTS.copy()):
            result=config.runtime_config()
            self.assertNotIn('DISCORD_GUILD_ID',result)
            self.assertEqual(set(config.REQUIRED),set(secrets))
    def test_legacy_guild_id_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'
            path.write_text(json.dumps({'DISCORD_GUILD_ID':'invalid','DISCORD_TOKEN':'preserved'}))
            result=config.read_config(path)
            self.assertNotIn('DISCORD_GUILD_ID',result)
            self.assertEqual(result['DISCORD_TOKEN'],'preserved')

    def test_old_image_model_migrates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'
            path.write_text(json.dumps({'IMAGE_MODEL':'@cf/black-forest-labs/flux-1-schnell','DISCORD_TOKEN':'preserved'}))
            result=config.read_config(path)
            self.assertEqual(result['IMAGE_MODEL'],'@cf/black-forest-labs/flux-2-klein-4b')
            self.assertEqual(result['DISCORD_TOKEN'],'preserved')
