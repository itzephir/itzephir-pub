from __future__ import annotations

from dataclasses import dataclass
import hashlib
import html
import json
from pathlib import Path
import re
import subprocess
import tomllib
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from telethon.extensions import html as telegram_html
import yaml


class PostError(ValueError):
    pass


class UniqueKeysLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node):
    pairs = loader.construct_pairs(node)
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise PostError('Frontmatter keys must be unique strings.')
        result[key] = value
    return result


UniqueKeysLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


@dataclass(frozen=True)
class Post:
    id: str
    status: str
    path: str
    markdown: str
    html: str
    text: str
    entities: tuple

    @property
    def digest(self):
        body = {'text': self.text, 'entities': [e.to_dict() for e in self.entities]}
        return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @property
    def source_digest(self):
        return hashlib.sha256((self.status + '\n' + self.markdown).encode()).hexdigest()


def configuration(root: Path):
    config = tomllib.loads((root / 'publisher.toml').read_text())
    if config.get('posts_dir') != 'posts':
        raise PostError('posts_dir must be posts (the CI trust boundary).')
    branch = config.get('state_branch', '')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+', branch) or branch == 'main':
        raise PostError('state_branch must be a simple branch name other than main.')
    return config


def render(markdown: str):
    parser = MarkdownIt('commonmark', {'html': False}).enable('strikethrough')
    tokens = parser.parse(markdown)
    output, lists = [], []
    quote_depth = 0
    for token in tokens:
        kind = token.type
        if kind == 'inline':
            for child in token.children or []:
                if child.type == 'image':
                    raise PostError('Inline images are not supported yet; use a text link.')
                if child.type == 'link_open':
                    url = child.attrGet('href') or ''
                    if urlsplit(url).scheme not in {'http', 'https', 'tg', 'mailto'}:
                        raise PostError('Links must be absolute http, https, tg, or mailto URLs.')
            output.append(parser.renderer.renderInline(token.children or [], parser.options, {}))
        elif kind in {'heading_open', 'heading_close'}:
            output.append('<b>' if kind == 'heading_open' else '</b>\n\n')
        elif kind == 'paragraph_close':
            if not token.hidden:
                output.append('\n\n')
        elif kind == 'paragraph_open':
            pass
        elif kind in {'bullet_list_open', 'ordered_list_open'}:
            if lists and output and not output[-1].endswith('\n'):
                output.append('\n')
            lists.append(None if kind == 'bullet_list_open' else int(token.attrGet('start') or 1))
        elif kind in {'bullet_list_close', 'ordered_list_close'}:
            lists.pop()
            if not lists:
                output.append('\n')
        elif kind == 'list_item_open':
            number = lists[-1]
            output.append('  ' * (len(lists) - 1) + ('• ' if number is None else f'{number}. '))
            if number is not None:
                lists[-1] += 1
        elif kind == 'list_item_close':
            output.append('\n')
        elif kind in {'fence', 'code_block'}:
            language = (token.info.split() or [''])[0]
            if not re.fullmatch(r'[\w+-]{1,40}', language):
                language = ''
            attr = f' class="language-{html.escape(language, quote=True)}"' if language else ''
            output.append(f'<pre><code{attr}>{html.escape(token.content.rstrip(chr(10)))}</code></pre>\n\n')
        elif kind == 'blockquote_open':
            if quote_depth:
                raise PostError('Nested blockquotes are not supported by ordinary Telegram messages.')
            quote_depth += 1
            output.append('<blockquote>')
        elif kind == 'blockquote_close':
            quote_depth -= 1
            output.append('</blockquote>\n\n')
        elif kind == 'hr':
            output.append('———\n\n')
        else:
            raise PostError(f'Unsupported Markdown block: {kind}.')
    rendered = ''.join(output).strip()
    text, entities = telegram_html.parse(rendered)
    if not text.strip():
        raise PostError('The post body must not be empty.')
    if len(text) > 4096:
        raise PostError(f'The rendered post is {len(text)} characters; the limit is 4096.')
    return rendered, text, tuple(entities)


def parse_post(path: str, source: str):
    if len(source.encode()) > 64 * 1024:
        raise PostError('Post source exceeds 64 KiB.')
    normalized = source.replace('\r\n', '\n')
    match = re.match(r'\A---\n(.*?)\n---(?:\n|\Z)(.*)\Z', normalized, flags=re.S)
    if not match:
        raise PostError('A post must start with YAML frontmatter delimited by ---.')
    try:
        meta = yaml.load(match[1], Loader=UniqueKeysLoader)
    except yaml.YAMLError as error:
        raise PostError('Invalid YAML frontmatter.') from error
    if not isinstance(meta, dict) or set(meta) != {'id', 'status'}:
        raise PostError('Frontmatter must contain exactly id and status.')
    if not isinstance(meta['id'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', meta['id']):
        raise PostError('id must be a stable lowercase slug, at most 80 characters.')
    if not isinstance(meta['status'], str) or meta['status'] not in {'draft', 'published'}:
        raise PostError('status must be draft or published.')
    markdown = match[2].strip() + '\n'
    rendered, text, entities = render(markdown)
    return Post(meta['id'], meta['status'], path, markdown, rendered, text, entities)


def git(root: Path, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=True)
    return result.stdout


def commit_sha(root: Path, ref: str):
    return git(root, 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}').decode().strip()


def load_posts(root: Path, ref: str | None = None):
    configuration(root)
    sources = []
    if ref:
        sha = commit_sha(root, ref)
        for entry in git(root, 'ls-tree', '-rz', sha, '--', 'posts/').split(b'\0'):
            if not entry:
                continue
            metadata, raw_path = entry.split(b'\t', 1)
            path = raw_path.decode('utf-8')
            if not path.endswith('.md'):
                continue
            if metadata.split()[0] != b'100644':
                raise PostError('Posts in Git must be regular, non-executable files.')
            sources.append((path, git(root, 'show', f'{sha}:{path}').decode('utf-8')))
    else:
        posts_dir = root / 'posts'
        for path in sorted(posts_dir.rglob('*.md')):
            if path.is_symlink() or not path.resolve().is_relative_to(posts_dir.resolve()):
                raise PostError('Post paths must not follow symlinks outside posts/.')
            sources.append((path.relative_to(root).as_posix(), path.read_text()))
    result = {}
    for path, source in sources:
        try:
            post = parse_post(path, source)
        except PostError as error:
            raise PostError(f'{path}: {error}') from error
        if post.id in result:
            raise PostError(f'Duplicate post ID: {post.id}.')
        result[post.id] = post
    return result


def select_posts(posts, target, previous=None):
    if previous is not None:
        old_paths = {p.path: p.id for p in previous.values()}
        for post in posts.values():
            if post.path in old_paths and old_paths[post.path] != post.id:
                raise PostError(f'{post.path}: the stable id cannot be changed in place.')
    if target == 'release':
        return [p for p in posts.values() if p.status == 'published']
    return [p for p in posts.values()
            if previous is None or p.id not in previous or p.source_digest != previous[p.id].source_digest]


def build_bundle(posts, output: Path, target: str, previous=None):
    selected = select_posts(posts, target, previous)
    output.mkdir(parents=True, exist_ok=True)
    manifest = {'version': 1, 'target': target, 'delivery': 'not_attempted', 'posts': [],
                'removed_ids': sorted(set(previous or {}) - set(posts))}
    summary = [f'# {target.capitalize()} bundle', '',
               'Telegram delivery has not been attempted.', '']
    for post in sorted(selected, key=lambda p: p.path):
        (output / f'{post.id}.telegram.html').write_text(post.html + '\n')
        (output / f'{post.id}.txt').write_text(post.text + '\n')
        manifest['posts'].append({'id': post.id, 'path': post.path, 'status': post.status,
                                  'sha256': post.digest, 'characters': len(post.text)})
        summary += [f'## {post.id} ({post.status})', '', post.markdown, '---', '']
    if not selected:
        summary += ['No posts selected.', '']
    if manifest['removed_ids']:
        summary += ['Removed source IDs (Telegram messages are kept): ' + ', '.join(manifest['removed_ids']), '']
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    (output / 'preview.md').write_text('\n'.join(summary))
    return selected
