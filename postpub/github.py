"""The small GitHub API surface needed for PR binding and durable message IDs."""
from __future__ import annotations

import base64
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .core import PostError


class GitHubError(PostError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class GitHub:
    def __init__(self, repository=None, token=None):
        self.repository = repository or os.environ.get('GITHUB_REPOSITORY', '')
        self.token = token or os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', self.repository) or not self.token:
            raise PostError('GITHUB_REPOSITORY and GITHUB_TOKEN are required for live delivery.')
        self.base = os.environ.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
        if not self.base.startswith('https://'):
            raise PostError('GitHub API transport must use HTTPS.')

    def request(self, method, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(self.base + f'/repos/{self.repository}/' + path,
                          data=body, method=method, headers={
                              'Authorization': f'Bearer {self.token}',
                              'Accept': 'application/vnd.github+json',
                              'Content-Type': 'application/json',
                              'User-Agent': 'itzephir-pub',
                          })
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            raise GitHubError(error.code, f'GitHub {method} {path.split("?")[0]} failed (HTTP {error.code}).') from None
        except (URLError, TimeoutError):
            raise GitHubError(0, 'GitHub request failed; a write outcome may be unknown.') from None

    def verified_pr(self, number: int, sha: str):
        if number < 1 or not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise PostError('A positive PR number and full commit SHA are required.')
        pull = self.request('GET', f'pulls/{number}')
        if pull['state'] != 'open' or pull['base']['ref'] != 'main':
            raise PostError('Preview delivery requires an open PR targeting main.')
        head_repo = pull['head'].get('repo') or {}
        if head_repo.get('full_name', '').lower() != self.repository.lower():
            raise PostError('Live Telegram previews are limited to same-repository PRs.')
        if pull['head']['sha'] != sha:
            raise PostError('This preview is stale; the PR head has changed.')
        return pull


class GitHubState:
    """Each save is a compare-and-swap contents commit on a separate branch."""
    path = 'publication-state.json'

    def __init__(self, api: GitHub, branch: str):
        self.api, self.branch, self.sha = api, branch, None
        try:
            self.data = self._read()
        except GitHubError as error:
            if error.status != 404:
                raise
            try:
                api.request('GET', 'git/ref/heads/' + quote(branch, safe=''))
            except GitHubError as branch_error:
                if branch_error.status != 404:
                    raise
            else:
                raise PostError('The state branch exists but its registry is missing; restore it before publishing.')
            main = api.request('GET', 'git/ref/heads/main')
            api.request('POST', 'git/refs', {'ref': 'refs/heads/' + branch, 'sha': main['object']['sha']})
            self.data = {'version': 1, 'destinations': {}}
            self.save()
        if self.data.get('version') != 1 or not isinstance(self.data.get('destinations'), dict):
            raise PostError('Unsupported or corrupt publication state.')

    def _read(self):
        document = self.api.request('GET', 'contents/' + self.path + '?ref=' + quote(self.branch, safe=''))
        self.sha = document['sha']
        try:
            return json.loads(base64.b64decode(document['content']))
        except (ValueError, KeyError):
            raise PostError('The publication registry is not valid JSON.') from None

    def save(self):
        payload = {'message': 'chore: update Telegram publication state', 'branch': self.branch,
                   'content': base64.b64encode((json.dumps(self.data, ensure_ascii=False, indent=2) + '\n').encode()).decode()}
        if self.sha:
            payload['sha'] = self.sha
        result = self.api.request('PUT', 'contents/' + self.path, payload)
        self.sha = result['content']['sha']


def preview_context(api, event):
    """Only a successful same-repository pull_request run may enter the privileged path."""
    run = event.get('workflow_run', {})
    if run.get('event') != 'pull_request' or run.get('conclusion') != 'success':
        raise PostError('A successful pull_request validation run is required.')
    pulls = run.get('pull_requests') or []
    if len(pulls) != 1:
        raise PostError('The validation run must identify exactly one PR.')
    number, sha = pulls[0]['number'], run.get('head_sha', '')
    api.verified_pr(number, sha)
    return number, sha
