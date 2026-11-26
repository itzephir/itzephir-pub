import base64
import json
import unittest

from postpub.core import PostError
from postpub.github import GitHubError, GitHubState


class FakeAPI:
    def __init__(self, branch_exists=False, document=None):
        self.branch_exists, self.document = branch_exists, document
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == 'GET' and path.startswith('contents/'):
            if self.document is None:
                raise GitHubError(404, 'Not found')
            return {'sha': 'old-blob', 'content': base64.b64encode(json.dumps(self.document).encode()).decode()}
        if path == 'git/ref/heads/telegram-state':
            if self.branch_exists:
                return {'object': {'sha': 'state-commit'}}
            raise GitHubError(404, 'Not found')
        if path == 'git/ref/heads/main':
            return {'object': {'sha': 'main-commit'}}
        if method == 'POST' and path == 'git/refs':
            return {}
        if method == 'PUT':
            return {'content': {'sha': 'new-blob'}}
        raise AssertionError((method, path))


class StateTests(unittest.TestCase):
    def test_registry_bootstrap_writes_only_to_state_branch(self):
        api = FakeAPI()
        state = GitHubState(api, 'telegram-state')
        self.assertEqual(state.data, {'version': 1, 'destinations': {}})
        writes = [call for call in api.calls if call[0] == 'PUT']
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][2]['branch'], 'telegram-state')

    def test_missing_registry_on_existing_branch_fails_closed(self):
        api = FakeAPI(branch_exists=True)
        with self.assertRaises(PostError):
            GitHubState(api, 'telegram-state')
        self.assertFalse(any(call[0] in {'PUT', 'POST'} for call in api.calls))

    def test_existing_registry_updates_with_optimistic_sha(self):
        api = FakeAPI(document={'version': 1, 'destinations': {}})
        state = GitHubState(api, 'telegram-state')
        state.save()
        self.assertEqual(api.calls[-1][2]['sha'], 'old-blob')
        self.assertEqual(state.sha, 'new-blob')


if __name__ == '__main__':
    unittest.main()
