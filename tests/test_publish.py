import copy
import unittest

from postpub.core import PostError
from postpub.github import GitHub, preview_context
from postpub.publish import deliver
from test_core import post


class Store:
    def __init__(self):
        self.data = {'version': 1, 'destinations': {}}
        self.saved = copy.deepcopy(self.data)
        self.saves = 0
        self.fail_on = None

    def save(self):
        self.saves += 1
        if self.saves == self.fail_on:
            raise OSError('Simulated lost state write')
        self.saved = copy.deepcopy(self.data)


class Sender:
    chat_id = -1001234567890
    account_id = 123

    def __init__(self, store):
        self.calls, self.store = [], store

    async def send(self, candidate, random_id):
        records = self.store.saved['destinations']['release']['posts']
        assert records[candidate.id]['status'] == 'pending'
        self.calls.append(('send', candidate.id, random_id))
        return 100

    async def edit(self, candidate, message_id):
        self.calls.append(('edit', candidate.id, message_id))
        return message_id


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_runs_skip_and_edits_reuse_message_id(self):
        store = Store()
        sender = Sender(store)
        first = post(status='published')
        await deliver([first], store, sender, 'release')
        await deliver([first], store, sender, 'release')
        await deliver([post('Changed', status='published')], store, sender, 'release')
        self.assertEqual([c[0] for c in sender.calls], ['send', 'edit'])
        self.assertEqual(sender.calls[1][2], 100)

    async def test_lost_commit_after_send_does_not_cause_duplicate(self):
        store = Store()
        store.fail_on = 2
        sender = Sender(store)
        first = post(status='published')
        with self.assertRaises(OSError):
            await deliver([first], store, sender, 'release')
        store.data = copy.deepcopy(store.saved)  # Simulate the next CI run.
        with self.assertRaises(PostError):
            await deliver([first], store, sender, 'release')
        self.assertEqual(len(sender.calls), 1)

    async def test_failed_intent_save_sends_nothing(self):
        store = Store()
        store.fail_on = 1
        sender = Sender(store)
        with self.assertRaises(OSError):
            await deliver([post()], store, sender, 'release')
        self.assertEqual(sender.calls, [])

    async def test_channel_change_is_not_treated_as_new_destination(self):
        store = Store()
        sender = Sender(store)
        await deliver([post()], store, sender, 'release')
        sender.chat_id -= 1
        with self.assertRaises(PostError):
            await deliver([post()], store, sender, 'release')
        self.assertEqual(len(sender.calls), 1)

    async def test_stale_guard_stops_before_intent_or_delivery(self):
        store = Store()
        sender = Sender(store)
        def guard():
            raise PostError('Stale PR')
        with self.assertRaises(PostError):
            await deliver([post()], store, sender, 'release', guard)
        self.assertEqual((store.saves, len(sender.calls)), (0, 0))


class PreviewBindingTests(unittest.TestCase):
    def test_run_must_be_successful_pull_request(self):
        with self.assertRaises(PostError):
            preview_context(None, {'workflow_run': {'event': 'push', 'conclusion': 'success'}})

    def test_current_pr_and_same_repository_are_verified(self):
        api = GitHub(repository='itzephir/itzephir-pub', token='test')
        pull = {'state': 'open', 'base': {'ref': 'main'},
                'head': {'sha': 'a' * 40, 'repo': {'full_name': 'itzephir/itzephir-pub'}}}
        api.request = lambda *args: pull
        self.assertEqual(api.verified_pr(1, 'a' * 40), pull)
        with self.assertRaises(PostError):
            api.verified_pr(1, 'b' * 40)
        pull['head']['repo']['full_name'] = 'someone/fork'
        with self.assertRaises(PostError):
            api.verified_pr(1, 'a' * 40)


if __name__ == '__main__':
    unittest.main()
