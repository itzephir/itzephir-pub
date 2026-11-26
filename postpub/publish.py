"""Personal-account delivery with durable in-flight records."""
from __future__ import annotations

import hashlib
import os

from telethon import TelegramClient, functions, types, utils
from telethon.sessions import StringSession

from .core import PostError


class Telegram:
    def __init__(self):
        required = ('TELEGRAM_API_ID', 'TELEGRAM_API_HASH', 'TELEGRAM_SESSION_STRING',
                    'TELEGRAM_CHAT_ID', 'TELEGRAM_EXPECTED_USER_ID')
        if any(not os.environ.get(key) for key in required):
            raise PostError('Telegram delivery is enabled but its environment is incomplete. See docs/telegram-setup.md.')
        try:
            self.expected_user_id = int(os.environ['TELEGRAM_EXPECTED_USER_ID'])
            api_id = int(os.environ['TELEGRAM_API_ID'])
        except ValueError:
            raise PostError('Telegram API ID and expected user ID must be integers.') from None
        self.reference = os.environ['TELEGRAM_CHAT_ID']
        self.client = TelegramClient(StringSession(os.environ['TELEGRAM_SESSION_STRING']), api_id,
                                     os.environ['TELEGRAM_API_HASH'],
                                     device_model='itzephir-pub GitHub Actions',
                                     flood_sleep_threshold=0, request_retries=0, connection_retries=1)

    async def __aenter__(self):
        await self.client.connect()
        try:
            if not await self.client.is_user_authorized():
                raise PostError('Telegram session is not authorized; interactive login is disabled in CI.')
            me = await self.client.get_me()
            if me.bot or me.id != self.expected_user_id:
                raise PostError('The session must belong to the configured personal Telegram account.')
            self.account_id = me.id
            reference = int(self.reference) if self.reference.lstrip('-').isdigit() else self.reference
            try:
                channel = await self.client.get_entity(reference)
            except ValueError:
                # StringSession has no persisted entity cache. Warm only enough of
                # the account's dialogs to find the configured private destination.
                channel = None
                if isinstance(reference, int):
                    async for dialog in self.client.iter_dialogs():
                        if dialog.id == reference:
                            channel = dialog.entity
                            break
                if channel is None:
                    raise PostError('The configured Telegram channel could not be resolved.') from None
            if not isinstance(channel, types.Channel) or not channel.broadcast:
                raise PostError('The destination must be a broadcast channel.')
            self.channel, self.chat_id = channel, utils.get_peer_id(channel)
            if not channel.signatures or not getattr(channel, 'signature_profiles', False):
                raise PostError('Enable channel signatures and author profiles before publishing as your personal account.')
            senders = await self.client(functions.channels.GetSendAsRequest(peer=channel))
            if not any(utils.get_peer_id(sender.peer) == me.id for sender in senders.peers):
                raise PostError('Telegram does not allow this personal account as the channel sender.')
            permissions = await self.client.get_permissions(channel, me)
            if not (permissions.is_creator or permissions.is_admin):
                raise PostError('The personal account must be an administrator of the destination.')
            return self
        except BaseException:
            await self.client.disconnect()
            raise

    async def __aexit__(self, *args):
        await self.client.disconnect()

    async def send(self, post, random_id):
        result = await self.client(functions.messages.SendMessageRequest(
            peer=self.channel, message=post.text, entities=list(post.entities),
            random_id=random_id, send_as=types.InputPeerSelf()))
        updates = getattr(result, 'updates', [])
        for update in updates:
            if isinstance(update, types.UpdateMessageID) and update.random_id == random_id:
                return update.id
        candidates = [update.message for update in updates
                      if isinstance(update, types.UpdateNewChannelMessage)
                      and utils.get_peer_id(update.message.peer_id) == self.chat_id
                      and update.message.from_id
                      and utils.get_peer_id(update.message.from_id) == self.account_id
                      and update.message.message == post.text]
        if len(candidates) == 1:
            return candidates[0].id
        raise PostError('Telegram accepted a send without returning its message ID; inspect the channel before recovery.')

    async def edit(self, post, message_id):
        previous = await self.client.get_messages(self.channel, ids=message_id)
        if previous is None or not previous.from_id or utils.get_peer_id(previous.from_id) != self.account_id:
            raise PostError('The existing message is missing or was not authored by the configured personal account.')
        await self.client.edit_message(self.channel, message_id, post.text,
                                       formatting_entities=list(post.entities), parse_mode=None)
        return message_id


async def deliver(posts, store, sender, namespace, guard=None):
    if not posts:
        return []
    destination = store.data['destinations'].setdefault(namespace, {
        'chat_id': sender.chat_id, 'account_id': sender.account_id, 'posts': {}})
    if (destination.get('chat_id'), destination.get('account_id')) != (sender.chat_id, sender.account_id):
        raise PostError('The channel/account differs from recorded publication state; refusing to republish.')
    records = destination['posts']
    result = []
    for post in sorted(posts, key=lambda p: p.path):
        old = records.get(post.id)
        if old and old.get('status') == 'pending':
            raise PostError(f'{post.id}: a previous delivery has an unknown outcome. See docs/recovery.md.')
        if old and (old.get('status') != 'sent' or not isinstance(old.get('message_id'), int)
                    or old['message_id'] < 1 or not isinstance(old.get('sha256'), str)):
            raise PostError(f'{post.id}: the stored publication record is corrupt.')
        if old and old['sha256'] == post.digest:
            result.append((post.id, 'unchanged', old['message_id']))
            continue
        if guard:
            guard()
        random_id = int.from_bytes(hashlib.sha256(
            f'{sender.chat_id}:{namespace}:{post.id}'.encode()).digest()[:8], 'big') & ((1 << 63) - 1)
        records[post.id] = {'status': 'pending', 'sha256': post.digest,
                            'message_id': old['message_id'] if old else None, 'random_id': random_id}
        store.save()  # Persist intent before contacting Telegram.
        if old:
            message_id = await sender.edit(post, old['message_id'])
            action = 'edited'
        else:
            message_id = await sender.send(post, random_id)
            action = 'published'
        records[post.id].update(status='sent', message_id=message_id)
        store.save()
        result.append((post.id, action, message_id))
    return result
