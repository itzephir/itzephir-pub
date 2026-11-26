from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from .core import PostError, build_bundle, configuration, load_posts
from .github import GitHub, GitHubError, GitHubState, preview_context
from .publish import Telegram, deliver


def parser():
    root = argparse.ArgumentParser(description='Build and optionally publish Markdown Telegram posts.')
    root.add_argument('--root', type=Path, default=Path.cwd())
    sub = root.add_subparsers(dest='command', required=True)
    sub.add_parser('check')
    for name in ('build', 'publish'):
        command = sub.add_parser(name)
        command.add_argument('--target', choices=['preview', 'release'], required=True)
        command.add_argument('--ref')
        command.add_argument('--base-ref')
        command.add_argument('--output', type=Path, default=Path('build/bundle'))
        if name == 'publish':
            command.add_argument('--pr', type=int)
            command.add_argument('--live', action='store_true', help='Actually contact Telegram; requires enabled repository variable.')
    sub.add_parser('preview-context')
    return root


async def publish(args, posts, previous, config):
    selected = build_bundle(posts, args.output, args.target, previous)
    if not args.live:
        print('Dry run: no GitHub state writes or Telegram requests.')
        return
    enabled = 'TELEGRAM_PREVIEW_ENABLED' if args.target == 'preview' else 'TELEGRAM_RELEASE_ENABLED'
    if os.environ.get(enabled) != 'true':
        raise PostError(f'Live delivery requires {enabled}=true.')
    if not args.ref:
        raise PostError('Live delivery requires an explicit source commit.')
    api = GitHub()
    guard = None
    if args.target == 'preview':
        if not args.pr or not args.base_ref:
            raise PostError('Live previews require --pr and --base-ref.')
        guard = lambda: api.verified_pr(args.pr, args.ref)
        guard()
        namespace = f'pr-{args.pr}'
    else:
        if os.environ.get('GITHUB_REF') != 'refs/heads/main':
            raise PostError('Release delivery is restricted to main.')
        namespace = 'release'
    if not selected:
        print('No posts selected; no Telegram connection or state changes needed.')
        return
    async with Telegram() as sender:
        state = GitHubState(api, config['state_branch'])
        result = await deliver(selected, state, sender, namespace, guard)
        (args.output / 'delivery.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        for post_id, action, message_id in result:
            print(f'{post_id}: {action} (message_id={message_id})')


def main():
    args = parser().parse_args()
    try:
        config = configuration(args.root)
        if args.command == 'preview-context':
            event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
            output = os.environ.get('GITHUB_OUTPUT')
            try:
                number, sha = preview_context(GitHub(), event)
            except GitHubError:
                raise
            except PostError as error:
                if output:
                    with open(output, 'a') as stream:
                        stream.write('eligible=false\n')
                print(f'Preview skipped: {error}')
                return
            if output:
                with open(output, 'a') as stream:
                    stream.write(f'eligible=true\npr={number}\nsha={sha}\n')
            print(f'Validated preview source: PR #{number}, {sha}.')
            return
        posts = load_posts(args.root, getattr(args, 'ref', None))
        if args.command == 'check':
            print(f'Validated {len(posts)} post(s).')
            return
        previous = load_posts(args.root, args.base_ref) if args.base_ref else None
        if args.command == 'build':
            selected = build_bundle(posts, args.output, args.target, previous)
            print(f'Built {len(selected)} {args.target} post(s) in {args.output}; delivery not attempted.')
        else:
            asyncio.run(publish(args, posts, previous, config))
    except PostError as error:
        print(f'Error: {error}', file=sys.stderr)
        raise SystemExit(1)
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        print(f'Operation failed ({type(error).__name__}); no automatic retry.', file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        # RPC exception strings can embed request arguments. Keep logs free of secrets.
        seconds = getattr(error, 'seconds', None)
        suffix = f'; wait at least {seconds} seconds' if isinstance(seconds, int) else ''
        print(f'Telegram operation failed ({type(error).__name__}){suffix}. Inspect state before retrying.', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
