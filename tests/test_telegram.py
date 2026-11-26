from datetime import datetime, timezone
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import types

from postpub.core import PostError
from postpub.publish import Telegram
from test_core import post


class PersonalAccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.me = SimpleNamespace(id=123, bot=False)
        self.channel = types.Channel(id=456, title='test', photo=types.ChatPhotoEmpty(),
                                     date=datetime.now(timezone.utc), broadcast=True,
                                     signatures=True, signature_profiles=True)
        self.client = AsyncMock()
        self.client.is_user_authorized.return_value = True
        self.client.get_me.return_value = self.me
        self.client.get_entity.return_value = self.channel
        self.client.get_permissions.return_value = SimpleNamespace(is_creator=True, is_admin=True)
        self.client.return_value = SimpleNamespace(peers=[SimpleNamespace(peer=types.PeerUser(123))])
        self.environment = patch.dict(os.environ, {
            'TELEGRAM_API_ID': '1', 'TELEGRAM_API_HASH': 'test', 'TELEGRAM_SESSION_STRING': 'test',
            'TELEGRAM_CHAT_ID': '@test', 'TELEGRAM_EXPECTED_USER_ID': '123',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.session_patch = patch('postpub.publish.StringSession', return_value=None)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.client_patch = patch('postpub.publish.TelegramClient', return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    async def test_bot_session_is_rejected_without_resolving_destination(self):
        self.me.bot = True
        with self.assertRaises(PostError):
            async with Telegram():
                self.fail('Bot session accepted')
        self.client.get_entity.assert_not_awaited()
        self.client.disconnect.assert_awaited_once()

    async def test_wrong_personal_account_is_rejected(self):
        self.me.id = 999
        with self.assertRaises(PostError):
            async with Telegram():
                self.fail('Wrong account accepted')

    async def test_invisible_personal_author_is_rejected(self):
        self.channel.signature_profiles = False
        with self.assertRaises(PostError):
            async with Telegram():
                self.fail('Missing personal profile visibility accepted')

    async def test_channel_identity_is_not_used_as_fallback(self):
        self.client.return_value = SimpleNamespace(peers=[SimpleNamespace(peer=types.PeerChannel(456))])
        with self.assertRaises(PostError):
            async with Telegram():
                self.fail('Channel sender used as fallback')

    async def test_raw_send_explicitly_selects_personal_account(self):
        async with Telegram() as sender:
            self.client.return_value = SimpleNamespace(updates=[types.UpdateMessageID(id=55, random_id=42)])
            self.assertEqual(await sender.send(post(), 42), 55)
            request = self.client.await_args.args[0]
            self.assertIsInstance(request.send_as, types.InputPeerSelf)
            self.assertEqual(request.random_id, 42)

    async def test_another_authors_message_is_not_edited(self):
        async with Telegram() as sender:
            self.client.get_messages.return_value = SimpleNamespace(from_id=types.PeerUser(999))
            with self.assertRaises(PostError):
                await sender.edit(post(), 55)
            self.client.edit_message.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
