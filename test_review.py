"""Review regressions. Run: python3 -m unittest -v"""

import json
import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import review


class CommentRangeTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {'t': 'hunk', 'text': '@@ -10,3 +10,3 @@'},
            {'t': 'ctx', 'k': 'R10', 'old': 10, 'new': 10, 'text': 'first'},
            {'t': 'del', 'k': 'L11', 'old': 11, 'text': 'old middle'},
            {'t': 'add', 'k': 'R11', 'new': 11, 'text': 'new middle'},
            {'t': 'ctx', 'k': 'R12', 'old': 12, 'new': 12, 'text': 'last'},
            {'t': 'hunk', 'text': '@@ -20 +20 @@'},
            {'t': 'ctx', 'k': 'R20', 'old': 20, 'new': 20, 'text': 'elsewhere'},
        ]

    def test_ranges_and_context_on_both_sides(self):
        for side, middle in [('L', 'old middle'), ('R', 'new middle')]:
            with self.subTest(side=side):
                selected = review.comment_range(self.rows, side + '10', side + '12')
                self.assertEqual([r['text'] for r in selected], ['first', middle, 'last'])
                context = review.thread_context(self.rows, side + '12', 0, side + '10')
                self.assertEqual(len([line for line in context if line.startswith('>>> ')]), 3)
                self.assertIn(middle, '\n'.join(line for line in context if line.startswith('>>> ')))
        self.assertEqual(review.comment_range(self.rows, 'R10', 'R20'), [])
        self.assertEqual(review.comment_range(self.rows, 'L10', 'R12'), [])
        self.assertEqual(review.comment_range(self.rows, 'R12', 'R10'), [])
        self.assertEqual(len(review.comment_range(self.rows, 'L12', 'L12')), 1)

    def test_range_persistence_legacy_edits_and_replies(self):
        with tempfile.TemporaryDirectory() as root:
            store = review.Store(root)
            comment = {'line': 'R12', 'start_line': 'R10', 'text': 'Review this block',
                       'snippet': 'last', 'range_snippet': 'first\nnew middle\nlast'}
            state = store.patch('a.py', {'comments': [comment, {'line': 'L11', 'text': 'Single line'}]})
            cid = state['comments'][0]['id']
            store.add_reply('a.py', cid, 'Acknowledged')
            # An older client editing the text must not discard range metadata.
            store.patch('a.py', {'comments': [{'id': cid, 'text': 'Edited'}]})
            saved = review.Store(root).file('a.py')['comments'][0]
            self.assertEqual(saved['start_line'], 'R10')
            self.assertEqual(saved['line'], 'R12')
            self.assertEqual(saved['range_snippet'], comment['range_snippet'])
            self.assertEqual(saved['replies'][0]['text'], 'Acknowledged')
            self.assertEqual(review.comment_location(saved), 'R10–R12')
            for start in ('L10', 'R13', 'R0', 'bad'):
                with self.subTest(start=start), self.assertRaises(ValueError):
                    store.patch('a.py', {'comments': [dict(comment, start_line=start)]})
            self.assertEqual(store.file('a.py')['comments'][0]['text'], 'Edited')

    @unittest.skipUnless(shutil.which('node'), 'Node is needed to test browser anchoring')
    def test_browser_range_selection_and_reanchoring(self):
        # Exercise the exact pure functions embedded in the delivered page.
        helpers = review.PAGE.split('const sideKey = ', 1)[1].split('function selectRange(', 1)[0]
        anchor = review.PAGE.split('function anchor(c, byKey){', 1)[1].split('function paintComments(', 1)[0]
        script = ('const assert = require("node:assert/strict");\nlet ROWS = ' + json.dumps(self.rows) + ';\n'
                  + 'const sideKey = ' + helpers + '\nfunction anchor(c, byKey){' + anchor + r'''
const comment = {start_line:'L10', line:'L12', snippet:'last', range_snippet:'first\nold middle\nlast'};
assert.deepEqual(commentRange('R12', 'R10').rows.map(r => r.k), ['R10','R11','R12']);
assert.equal(commentRange('R10', 'R20'), null);
assert.equal(commentRange('L10', 'R12'), null);
assert.equal(anchor(comment, {}).key, 'R12');
ROWS = ROWS.map(r => ({...r, ...(r.old ? {old:r.old + 5} : {}), ...(r.new ? {new:r.new + 5} : {}),
                     ...(r.k ? {k:r.k[0] + (+r.k.slice(1) + 5)} : {})}));
const moved = anchor(comment, {});
assert.equal(moved.start, 'L15');
assert.equal(moved.end, 'L17');
assert.equal(moved.from, 'L10–L12');
assert.equal(anchor({line:'L17', snippet:'last'}, {'L17':ROWS[4]}).key, 'R17');
ROWS[2].text = 'changed inside the selected range';
assert.equal(anchor(comment, {}), null);
''')
        result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class DefinitionSyntaxTests(unittest.TestCase):
    def test_common_declarations(self):
        examples = {
            'py': 'async def target(\n    value\n):\n    return target(value)\n',
            'java': 'public static String target(\n    String value\n) throws Exception {\n    return target(value);\n}\n',
            'ts': 'export const target = (value: string) => value;\ntarget("x");\n',
            'js': 'async function target(value) {\n  return target(value);\n}\n',
            'go': 'func (s *Server) target(value string) error {\n  return target(value)\n}\n',
            'rs': 'pub async fn target(value: String) {\n  target(value);\n}\n',
            'kt': 'fun target(value: String): String {\n  return target(value)\n}\n',
            'cpp': 'static int target(int value) {\n  return target(value);\n}\n',
        }
        for ext, source in examples.items():
            with self.subTest(ext=ext):
                self.assertEqual(review.definition_lines(source, 'code.' + ext, 'target'), [1])

    def test_python_binding_and_comments(self):
        source = ('# def target():\n"def target():"\nclass Target:\n'
                  '    def target(self):\n        return target()\n'
                  'Target.value = 1\na, target = (1, 2)\n')
        self.assertEqual(review.definition_lines(source, 'code.py', 'target'), [4, 7])
        self.assertEqual(review.definition_lines(source, 'code.py', 'Target'), [3])

    def test_methods_overloads_and_non_definitions(self):
        source = ('// void target() {}\n/*\nvoid target() {}\n*/\n'
                  'String example = "void target() {}";\n'
                  'public void target(String x) {}\n'
                  'public void target(int x);\n'
                  '  target();\n  return target();\n  new target();\n')
        self.assertEqual(review.definition_lines(source, 'Example.java', 'target'), [6, 7])
        self.assertEqual(review.definition_lines('  async target(x: X): Promise<X> {\n}', 'x.ts', 'target'), [1])
        self.assertEqual(review.definition_lines('@Override public <T> java.util.List<T> target(T x) {\n}', 'x.java', 'target'), [1])
        self.assertEqual(review.definition_lines('export default target;\n', 'x.js', 'target'), [])

    def test_incomplete_python_fallback(self):
        self.assertEqual(review.definition_lines('def target():\n    broken(\n', 'x.py', 'target'), [1])


class RepositoryNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        review.git(str(self.repo), 'init', '-q')
        review.git(str(self.repo), 'config', 'user.email', 'test@example.com')
        review.git(str(self.repo), 'config', 'user.name', 'Test')
        self.write('caller.py', 'from helpers import target\ntarget()\n')
        self.write('helpers.py', 'def target():\n    return "old"\n')
        self.write('duplicate.py', 'def duplicate():\n    pass\n')
        self.write('removed.py', 'def removed():\n    pass\n')
        self.write('.gitignore', 'ignored.py\n.review/\n')
        self.commit()
        self.base = review.git(str(self.repo), 'rev-parse', 'HEAD').strip()
        self.write('caller.py', 'from helpers import target\ntarget(1)\n')
        self.write('helpers.py', '\n\ndef target(value):\n    return "head"\n')
        review.git(str(self.repo), 'mv', 'removed.py', 'renamed.py')
        self.commit()
        self.head = review.git(str(self.repo), 'rev-parse', 'HEAD').strip()
        self.ctx = dict(root=str(self.repo), base=self.base, head=self.head,
                        worktree=True, untracked=True,
                        files=[dict(path='caller.py', dspec=self.base + '..' + self.head),
                               dict(path='renamed.py', old='removed.py', dspec=self.base + '..' + self.head)])

    def write(self, path, text):
        (self.repo / path).write_text(text)

    def commit(self):
        review.git(str(self.repo), 'add', '.')
        review.git(str(self.repo), 'commit', '-qm', 'fixture')

    def lookup(self, symbol='target', side='R'):
        return review.find_definitions(self.ctx, 'caller.py', side, symbol)

    def test_committed_sides_ignore_uncommitted_content(self):
        self.write('helpers.py', '\n\n\ndef target(value):\n    return "dirty"\n')
        self.assertEqual(self.lookup()['matches'][0]['line'], 3)
        self.assertEqual(self.lookup(side='L')['matches'][0]['line'], 1)
        self.assertEqual(self.lookup()['revision'], self.head)

    def test_worktree_unchanged_files_untracked_and_duplicates(self):
        self.ctx['files'][0]['dspec'] = self.base
        self.write('helpers.py', '\n\n\ndef target(value):\n    return "dirty"\n')
        self.write('extra.py', 'def target():\n    return 2\n')
        self.write('ignored.py', 'def target():\n    return 3\n')
        result = self.lookup()
        self.assertEqual({(m['path'], m['line']) for m in result['matches']},
                         {('helpers.py', 4), ('extra.py', 1)})
        self.assertEqual(result['revision'], 'working tree')
        self.ctx['untracked'] = False
        self.assertEqual([m['path'] for m in self.lookup()['matches']], ['helpers.py'])
        self.assertEqual(self.lookup(side='L')['matches'][0]['line'], 1)

    def test_renamed_source_in_old_snapshot(self):
        result = review.find_definitions(self.ctx, 'renamed.py', 'L', 'removed')
        self.assertEqual(result['matches'][0]['path'], 'removed.py')
        self.assertIn('def removed', review.source_text(str(self.repo), 'removed.py', self.base))
        with self.assertRaises(ValueError):
            review.source_text(str(self.repo), 'removed.py', self.head)

    def test_dollar_symbols_and_literal_filenames(self):
        self.ctx['files'][0]['dspec'] = self.base
        self.write('has [brackets].js', 'const $target = () => 1;\nconst targetSuffix = 2;\n')
        self.assertEqual(self.lookup('$target')['matches'][0]['path'], 'has [brackets].js')
        self.assertIn('$target', review.source_text(str(self.repo), 'has [brackets].js', None))
        self.commit()
        revision = review.git(str(self.repo), 'rev-parse', 'HEAD').strip()
        self.assertIn('$target', review.source_text(str(self.repo), 'has [brackets].js', revision))

    def test_merge_base_and_head_fallback(self):
        review.git(str(self.repo), 'checkout', '-qb', 'other', self.base)
        self.write('other.py', 'pass\n')
        self.commit()
        other = review.git(str(self.repo), 'rev-parse', 'HEAD').strip()
        self.ctx['files'][0]['dspec'] = other + '...' + self.head
        self.assertEqual(review.source_revision(self.ctx, 'caller.py', 'L'), self.base)
        self.assertEqual(review.source_revision(self.ctx, 'caller.py', 'R'), self.head)
        self.ctx['files'][0]['dspec'] = 'HEAD'
        self.assertEqual(review.source_revision(self.ctx, 'caller.py', 'L'), other)
        self.assertIsNone(review.source_revision(self.ctx, 'caller.py', 'R'))

    def test_validation_and_non_repository_files(self):
        self.assertEqual(self.lookup('missing')['matches'], [])
        for symbol in ('', '-e', 'target()', 'a' * 201):
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                self.lookup(symbol)
        for path in ('../outside', '/etc/passwd', '.git/config', 'ignored.py'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                review.source_text(str(self.repo), path, None)
        (self.repo / 'link.py').symlink_to(self.repo / 'helpers.py')
        with self.assertRaises(ValueError):
            review.source_text(str(self.repo), 'link.py', None)
        self.write('large.py', 'x' * (review.MAX_SOURCE_BYTES + 1))
        self.write('binary.py', '\0data')
        for path in ('large.py', 'binary.py'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                review.source_text(str(self.repo), path, None)
        self.commit()
        revision = review.git(str(self.repo), 'rev-parse', 'HEAD').strip()
        with self.assertRaises(ValueError):
            review.source_text(str(self.repo), 'link.py', revision)

    def test_http_lookup_source_and_errors(self):
        handler = type('TestHandler', (review.Handler,), {'ctx': self.ctx})
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:%d' % server.server_port
        with urllib.request.urlopen(url + '/api/definitions?origin=caller.py&side=R&symbol=target') as response:
            self.assertEqual(json.load(response)['matches'][0]['path'], 'helpers.py')
        with urllib.request.urlopen(url + '/api/source?origin=caller.py&side=L&path=helpers.py') as response:
            self.assertEqual(json.load(response)['lines'][0], 'def target():')
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(url + '/api/source?origin=caller.py&side=R&path=../outside')
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_cli_reports_the_whole_comment_range(self):
        self.write('caller.py', 'first = 1\nsecond = 2\nthird = 3\nfourth = 4\n')
        store = review.Store(str(self.repo))
        store.meta(base=self.base, head=self.head, range=self.base + '..' + self.head, worktree=True)
        store.patch('caller.py', {'comments': [{'start_line': 'R2', 'line': 'R4',
                    'snippet': 'fourth = 4', 'range_snippet': 'second = 2\nthird = 3\nfourth = 4',
                    'text': 'Consider these three lines'}]})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            review.cmd_pending(['--repo', str(self.repo), '--all', '--context', '0'])
        comment = json.loads(output.getvalue())[0]
        self.assertEqual((comment['start_line'], comment['line']), ('R2', 'R4'))
        self.assertEqual(len([line for line in comment['context'] if line.startswith('>>> ')]), 3)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            review.cmd_todo(['--repo', str(self.repo)])
        self.assertIn('R2–R4', output.getvalue())


if __name__ == '__main__':
    unittest.main()
