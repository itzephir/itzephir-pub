import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from postpub.core import PostError, build_bundle, load_posts, parse_post, select_posts


def post(body='Hello **world**', post_id='hello', status='draft', path='posts/hello.md'):
    return parse_post(path, f'---\nid: {post_id}\nstatus: {status}\n---\n\n{body}\n')


class MarkdownTests(unittest.TestCase):
    def test_real_telegram_entities_and_utf16_offsets(self):
        result = post('😀 **Привет** и [ссылка](https://example.com/?a=1&b=2)')
        self.assertEqual(result.text, '😀 Привет и ссылка')
        bold = next(entity for entity in result.entities if type(entity).__name__ == 'MessageEntityBold')
        self.assertEqual((bold.offset, bold.length), (3, 6))
        link = next(entity for entity in result.entities if type(entity).__name__ == 'MessageEntityTextUrl')
        self.assertEqual(link.url, 'https://example.com/?a=1&b=2')

    def test_headings_lists_and_fenced_code(self):
        result = post('# Heading\n\n- one\n- **two**\n\n```python\nprint("<hello>")\n```')
        self.assertIn('Heading\n\n• one\n• two', result.text)
        self.assertIn('print("<hello>")', result.text)
        pre = next(entity for entity in result.entities if type(entity).__name__ == 'MessageEntityPre')
        self.assertEqual(pre.language, 'python')

    def test_nested_lists_preserve_item_boundaries(self):
        self.assertIn('• parent\n  • child\n', post('- parent\n  - child\n- last').text)

    def test_raw_html_stays_literal(self):
        self.assertEqual(post('<b>literal</b>').text, '<b>literal</b>')

    def test_invalid_posts_fail_before_delivery(self):
        sources = [
            'no frontmatter',
            '---\nid: hello\nstatus: published\nstatus: draft\n---\nHello',
            '---\nid: hello\nstatus: later\n---\nHello',
            '---\nid: hello\nstatus: draft\nchannel: other\n---\nHello',
        ]
        for source in sources:
            with self.subTest(source=source), self.assertRaises(PostError):
                parse_post('posts/bad.md', source)
        for body in ('', 'x' * 4097, '![photo](photo.png)', '[relative](../secret)', '> > nested'):
            with self.subTest(body=body[:40]), self.assertRaises(PostError):
                post(body)

    def test_source_moves_do_not_change_message_payload(self):
        self.assertEqual(post().digest, post(path='posts/renamed.md').digest)

    def test_drafts_preview_but_do_not_release(self):
        posts = {'draft': post(post_id='draft'), 'ready': post(post_id='ready', status='published')}
        self.assertEqual(len(select_posts(posts, 'preview')), 2)
        self.assertEqual([p.id for p in select_posts(posts, 'release')], ['ready'])

    def test_preview_diff_and_immutable_ids(self):
        before = {'hello': post()}
        self.assertEqual(select_posts({'hello': post(path='posts/moved.md')}, 'preview', before), [])
        self.assertEqual(len(select_posts({'hello': post('New text')}, 'preview', before)), 1)
        with self.assertRaises(PostError):
            select_posts({'other': post(post_id='other')}, 'preview', before)

    def test_bundle_is_offline_and_keeps_deleted_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            build_bundle({}, Path(directory), 'preview', {'hello': post()})
            manifest = json.loads((Path(directory) / 'manifest.json').read_text())
            self.assertEqual(manifest['removed_ids'], ['hello'])
            self.assertEqual(manifest['delivery'], 'not_attempted')


class SourceTests(unittest.TestCase):
    def test_git_source_is_read_as_data_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'posts').mkdir()
            (root / 'publisher.toml').write_text('posts_dir = "posts"\nstate_branch = "telegram-state"\n')
            (root / 'posts/hello.md').write_text('---\nid: hello\nstatus: draft\n---\nHello')
            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)
            git('init', '-b', 'main')
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-m', 'initial')
            (root / 'posts/hello.md').write_text('not valid')
            self.assertEqual(load_posts(root, 'HEAD')['hello'].text, 'Hello')
            (root / 'posts/hello.md').unlink()
            (root / 'posts/hello.md').symlink_to(root / 'publisher.toml')
            with self.assertRaises(PostError):
                load_posts(root)


if __name__ == '__main__':
    unittest.main()

