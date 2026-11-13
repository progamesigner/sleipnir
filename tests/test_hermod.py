import copy
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'rootfs/usr/local/bin/sleipnir-hermod'
loader = importlib.machinery.SourceFileLoader('hermod', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
h = importlib.util.module_from_spec(spec)
loader.exec_module(h)


class HermodTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.cwd = self.path / 'project'
        self.cwd.mkdir()
        (self.cwd / '.git').mkdir()
        self.db = h.connect_db(self.path / 'state')
        self.addCleanup(self.db.close)
        self.term = dict(address='t1', created_ms=123, state='live', cwd=str(self.cwd),
                         foreground={'name': 'zsh', 'argv': ['zsh']},
                         labels={'herdr.space': 's1', 'herdr.tab': 'tab1',
                                 'where': 'Project / Work', 'shepherd.pane-name': 'Agent',
                                 'herdr.pinned': 'true'})
        self.event = dict(kind='start', agent='claude', session_id='session-123', cwd=str(self.cwd),
                          terminal='t1', at=h.now(), args=['--model', 'haiku', 'prompt'])
        self.r = h.Reconciler(self.db, self.path / 'state')
        self.r.terminals = [self.term]

    def start(self, source='one.json'):
        self.r.apply_inbox(self.event, source)
        return h.panes(self.db)[0]

    def test_schema_and_inbox_idempotency(self):
        pane = self.start()
        self.r.apply_inbox(self.event, 'one.json')
        self.assertEqual(len(h.panes(self.db)), 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM event').fetchone()[0], 1)
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 1)
        self.assertEqual(json.loads(pane['launch_args']), ['--model', 'haiku'])
        self.assertEqual(self.db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')

    def test_where(self):
        self.assertEqual(h.parse_where('Space / Tab / extra'), ('Space', 'Tab / extra'))
        self.assertEqual(h.parse_where('Space / 123'), ('Space', None))
        self.assertEqual(h.parse_where('Space / １２３'), ('Space', None))
        self.assertEqual(h.parse_where('Space'), ('Space', None))

    def test_cwd_git_worktree_submodule_and_unknown(self):
        nested = self.cwd / 'src'
        nested.mkdir()
        self.assertEqual(h.cwd_parts(str(nested)), (str(self.cwd), 'src'))
        worktree = self.cwd / '.worktrees/topic'
        worktree.mkdir(parents=True)
        (worktree / '.git').write_text('gitdir: ../../.git/worktrees/topic\n')
        self.assertEqual(h.cwd_parts(str(worktree)), (str(self.cwd), '.worktrees/topic'))
        external = self.path / 'external'
        external.mkdir()
        (external / '.git').write_text(f'gitdir: {self.cwd}/.git/worktrees/external\n')
        self.assertEqual(h.cwd_parts(str(external)), (str(self.cwd), str(external)))
        submodule = self.cwd / 'module'
        submodule.mkdir()
        (submodule / '.git').write_text(f'gitdir: {self.cwd}/.git/modules/module\n')
        self.assertEqual(h.cwd_parts(str(submodule)), (str(submodule), ''))
        self.assertEqual(h.cwd_parts(str(self.path)), (str(self.path), ''))

    def test_classification(self):
        cases = [({'kind': 'end', 'agent': 'claude', 'reason': 'prompt_input_exit'}, 'closed'),
                 ({'kind': 'end', 'agent': 'claude', 'reason': 'clear'}, 'observe'),
                 ({'kind': 'launch-exit', 'returncode': 0}, 'closed'),
                 ({'kind': 'launch-exit', 'returncode': 1}, 'closed'),
                 ({'kind': 'launch-exit', 'returncode': -9}, 'crash'),
                 ({'kind': 'finished', 'reason': 'killed'}, 'closed'),
                 ({'kind': 'lost', 'reason': 'signaled'}, 'crash'),
                 ({'kind': 'live', 'reason': 'stopped_by_restart'}, 'observe'),
                 ({'kind': 'foreground'}, 'observe'), ({'kind': 'missed'}, 'resnapshot')]
        for event, expected in cases:
            with self.subTest(event=event):
                self.assertEqual(h.classify(event), expected)
        event = dict(kind='end', agent='copilot', reason='user_exit')
        self.assertEqual(h.classify(event), 'pending-exit')
        self.assertEqual(h.classify(event, process_alive=False), 'closed')

    def test_phase1_mapping_incarnation_and_labels(self):
        pane = self.start()
        self.assertEqual(h.phase1_plan(pane, [self.term])['action'], 'map')
        changed = dict(self.term, created_ms=124)
        plan = h.phase1_plan(pane, [changed])
        self.assertEqual(plan['action'], 'create')
        self.assertFalse(plan['split'])
        self.assertEqual(plan['labels']['herdr.tab'], 'tab1')
        known_plan = h.phase1_plan(pane, [], {'tab1'})
        self.assertFalse(known_plan['split'])
        self.assertEqual(known_plan['labels']['herdr.tab'], 'tab1')
        unknown_plan = h.phase1_plan(pane, [changed], set())
        self.assertTrue(unknown_plan['split'])
        plan = h.phase1_plan(pane, [])
        self.assertTrue(plan['split'])
        self.assertEqual(plan['labels']['shepherd.tab-name'], 'Work · Agent')
        self.assertEqual(plan['labels']['herdr.pinned'], 'true')
        self.assertFalse(any(key.startswith('sleipnir.') for key in plan['labels']))
        self.db.execute('UPDATE tab SET name=NULL')
        self.assertNotIn('shepherd.tab-name', h.phase1_plan(h.panes(self.db)[0], [])['labels'])
        missing = dict(pane, subpath='missing')
        self.assertEqual(h.phase1_plan(missing, [])['action'], 'blocked')

    def test_phase2_concurrency_and_session_claims(self):
        pane = dict(self.start())
        rows, terms = [], []
        for i in range(6):
            rows.append(dict(pane, id=str(i), herdr_terminal_id=f't{i}'))
            terms.append(dict(self.term, address=f't{i}'))
        selected = h.phase2_plan(rows, terms, {}, set(str(i) for i in range(6)), lambda *_: False)
        self.assertEqual(selected, ['0', '1', '2', '3'])
        selected = h.phase2_plan(rows, terms, {'0': {}}, set(str(i) for i in range(6)), lambda *_: False, 2)
        self.assertEqual(selected, ['1'])
        self.assertEqual(h.phase2_plan(rows, terms, {}, {'0'}, lambda *_: True), [])
        terms[0]['foreground'] = {'name': 'claude', 'argv': ['claude']}
        self.assertEqual(h.phase2_plan(rows, terms, {}, {'0'}, lambda *_: False), [])

    def test_migration_newest_duplicate_finished_and_clear_after_commit(self):
        old = dict(agent='claude', session='old-session', cwd=str(self.cwd), at=1000, args=[])
        one, two, finished = copy.deepcopy(self.term), copy.deepcopy(self.term), copy.deepcopy(self.term)
        one['labels']['agent-resume'] = json.dumps(old)
        two.update(address='t2', created_ms=124)
        two['labels']['agent-resume'] = json.dumps(dict(old, at=2000))
        finished.update(address='t3', state='finished')
        finished['labels']['agent-resume'] = json.dumps(dict(old, session='finished-session'))
        clear = h.migrate_labels(self.db, [one, two, finished])
        self.assertEqual(clear, ['t1', 't2'])
        self.assertEqual(h.panes(self.db)[0]['herdr_terminal_id'], 't2')
        reader = h.connect_db(self.path / 'state', readonly=True)
        self.assertEqual(len(h.panes(reader)), 1)
        reader.close()
        self.assertEqual(h.migrate_labels(self.db, [one, two]), ['t1', 't2'])
        one['labels'].pop('agent-resume')
        two['labels'].pop('agent-resume')
        self.assertEqual(h.migrate_labels(self.db, [one, two]), [])

    def test_session_rotation_stale_exit_and_explicit_close(self):
        pane = self.start()
        newer = dict(self.event, session_id='session-new', at=h.now() + 10)
        self.r.apply_inbox(newer, 'two.json')
        self.assertEqual(h.panes(self.db)[0]['session_id'], 'session-new')
        self.r.apply_inbox(dict(kind='launch-exit', terminal='t1', session_id='session-123',
                                returncode=0, created_ms=123), 'old-exit.json')
        self.assertEqual(h.panes(self.db)[0]['state'], 'open')
        self.r.apply_inbox(dict(kind='close', pane_id=pane['id']), 'close.json')
        self.assertEqual(h.panes(self.db)[0]['state'], 'closed')
        self.r.apply_inbox(self.event, 'late-start.json')
        self.assertEqual(len(h.panes(self.db)), 1)
        self.assertEqual(h.panes(self.db)[0]['state'], 'closed')

    def test_crash_loop_and_retry(self):
        pane = self.start()
        for i in range(3):
            self.r.apply_inbox(dict(kind='launch-exit', terminal='t1', session_id='session-123',
                                    returncode=-9, created_ms=123), f'crash-{i}.json')
        self.assertEqual(h.panes(self.db)[0]['state'], 'blocked')
        self.r.apply_inbox(dict(kind='retry', pane_id=pane['id']), 'retry.json')
        self.assertEqual(h.panes(self.db)[0]['state'], 'open')
        self.assertIn(pane['id'], self.r.eligible)
        self.r.apply_inbox(dict(kind='launch-exit', terminal='t1', session_id='session-123',
                                returncode=-9, created_ms=123), 'after-retry.json')
        self.assertEqual(h.panes(self.db)[0]['state'], 'open')

    def test_core_close_matches_incarnation_and_crash_waits_for_restart(self):
        pane = self.start()
        self.r.observe(dict(kind='finished', reason='killed', terminal=dict(self.term, created_ms=999)))
        self.assertEqual(h.panes(self.db)[0]['state'], 'open')
        self.r.observe(dict(kind='lost', reason='signaled', terminal=self.term))
        self.assertNotIn(pane['id'], self.r.eligible)
        self.r.observe(dict(kind='restarted', terminal=self.term))
        self.assertIn(pane['id'], self.r.eligible)
        self.r.observe(dict(kind='finished', reason='killed', terminal=self.term))
        self.assertEqual(h.panes(self.db)[0]['state'], 'closed')

    def test_install_preserves_hook_order_and_removes_old_entries(self):
        path = self.path / 'hooks.json'
        path.write_text(json.dumps({'hooks': {'SessionStart': [
            {'matcher': 'startup', 'hooks': [{'type': 'command', 'command': 'user-first'},
                                            {'command': 'sleipnir-agent-resume hook codex'}]},
            {'hooks': [{'command': 'user-second'}]}]}}))
        h.install_hooks(path, 'codex', [('SessionStart', 'start'), ('SessionEnd', 'end')])
        first = path.read_text()
        h.install_hooks(path, 'codex', [('SessionStart', 'start'), ('SessionEnd', 'end')])
        self.assertEqual(path.read_text(), first)
        hooks = json.loads(first)['hooks']
        self.assertEqual(hooks['SessionStart'][0]['hooks'][0]['command'], 'user-first')
        self.assertEqual(len(hooks['SessionStart'][0]['hooks']), 1)
        self.assertEqual(hooks['SessionStart'][1]['hooks'][0]['command'], 'user-second')
        self.assertNotIn('matcher', hooks['SessionStart'][-1])
        self.assertEqual(h.hook_command('claude', 'start'), 'sleipnir-hermod hook claude start')
        result = subprocess.run(['python3', str(SCRIPT), 'hook', 'claude', 'start'],
                                input=b'invalid json', env=dict(os.environ, SLEIPNIR_ENABLE_HERMOD='1'),
                                capture_output=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))

    def test_atomic_inbox_and_hook_redaction(self):
        with patch.dict(os.environ, {'HC_TERMINAL_ID': 't1'}):
            value = h.read_hook('claude', 'start', json.dumps(dict(self.event, prompt='secret', transcript='secret')))
        self.assertNotIn('prompt', value)
        self.assertNotIn('transcript', value)
        h.enqueue(value, self.path / 'state')
        files = list((self.path / 'state/inbox').iterdir())
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.endswith('.json'))
        self.r.process_inbox()
        self.assertEqual(list((self.path / 'state/inbox').iterdir()), [])
        self.assertEqual(len(h.panes(self.db)), 1)

    def test_launch_ack_and_timeout_do_not_duplicate(self):
        pane = self.start()
        self.r.eligible.add(pane['id'])
        calls = []
        self.r.core = lambda *args, **kwargs: calls.append(args) or 'Trust this directory?'
        with patch.object(h, 'session_held', return_value=False):
            self.r.phase2()
            self.r.phase2()
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][:2], ('send', 't1'))
            self.assertTrue(calls[0][2].startswith(' sleipnir-hermod launch '))
            self.r.attempts[pane['id']]['deadline'] = 0
            self.r.phase2()
            self.assertEqual(self.r.runtime[pane['id']], 'awaiting_user')
            self.assertEqual(len(self.r.attempts), 0)
            self.r.apply_inbox({'kind': 'retry', 'pane_id': pane['id']}, 'retry.json')
            self.r.phase2()
            self.r.apply_inbox(dict(self.event, at=h.now() + 100), 'ack.json')
            self.assertNotIn(pane['id'], self.r.attempts)
            self.assertEqual(self.r.runtime[pane['id']], 'running')

    def test_restarted_hermod_counts_unacknowledged_agents_as_starting(self):
        pane = self.start()
        with self.db:
            h.record(self.db, 'launch', pane['id'])
        self.term['foreground'] = {'name': 'claude', 'argv': ['claude']}
        self.r.phase2()
        self.assertIn(pane['id'], self.r.attempts)
        self.assertEqual(self.r.runtime[pane['id']], 'starting')
        self.r.apply_inbox(dict(self.event, at=h.now() + 100), 'ack.json')
        self.assertNotIn(pane['id'], self.r.attempts)

    def test_drag_updates_tab_and_keeps_workspace_name(self):
        self.start()
        moved = copy.deepcopy(self.term)
        moved['labels'].update({'herdr.tab': 'tab2', 'where': 'Renamed / New tab'})
        self.r.refresh([moved])
        pane = h.panes(self.db)[0]
        self.assertEqual(pane['herdr_tab_id'], 'tab2')
        self.assertEqual(pane['workspace_name'], 'Project')
        self.assertEqual(pane['tab_name'], 'New tab')

    def test_rpc_event_shape_and_unknown_pane_control(self):
        self.start()
        self.r.observe({'jsonrpc': '2.0', 'method': 'event', 'params': {
            'seq': 1, 'type': 'finished', 'terminal': 't1', 'data': {'reason': 'killed'}}})
        self.assertEqual(h.panes(self.db)[0]['state'], 'closed')
        self.r.apply_inbox({'kind': 'retry', 'pane_id': 'missing'}, 'missing.json')
        self.assertEqual(self.db.execute("SELECT kind FROM event WHERE source_id='missing.json'").fetchone()[0], 'retry')

    def test_start_waits_for_snapshot_and_transaction_failure_keeps_inbox(self):
        self.r.terminals = []
        h.enqueue(self.event, self.path / 'state')
        self.r.process_inbox()
        self.assertEqual(len(list((self.path / 'state/inbox').glob('*.json'))), 1)
        self.r.terminals = [self.term]
        with patch.object(h, 'record', side_effect=h.sqlite3.OperationalError('disk full')):
            with self.assertRaises(h.sqlite3.OperationalError):
                self.r.process_inbox()
        self.assertEqual(len(h.panes(self.db)), 0)
        self.assertEqual(len(list((self.path / 'state/inbox').glob('*.json'))), 1)
        self.r.process_inbox()
        self.assertEqual(len(h.panes(self.db)), 1)

    def test_subagent_capture(self):
        with patch.dict(os.environ, {'HC_TERMINAL_ID': 't1'}):
            self.assertIsNone(h.read_hook('claude', 'start', json.dumps(dict(self.event, agent_id='child'))))
        with patch.dict(os.environ, {'HC_TERMINAL_ID': 't1', 'CODEX_THREAD_ID': 'parent-session'}):
            self.assertIsNone(h.read_hook('codex', 'start', json.dumps(self.event)))

    def test_core_readiness_uses_socket_response_without_starting_core(self):
        check = SCRIPT.parents[3] / 'etc/s6-overlay/s6-rc.d/herdr-core/data/check'
        endpoint = self.path / 'core.sock'
        import threading
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(endpoint))
            server.listen()
            def reply():
                with server.accept()[0] as client:
                    request = json.loads(client.makefile('rb').readline())
                    self.assertEqual(request['method'], 'server.status')
                    client.sendall(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
            thread = threading.Thread(target=reply)
            thread.start()
            env = dict(os.environ, HC_SOCKET=str(endpoint))
            result = subprocess.run(['python3', str(check)], env=env, capture_output=True, timeout=5)
            thread.join(5)
            self.assertEqual(result.returncode, 0, result.stderr)
        endpoint.unlink()
        result = subprocess.run(['python3', str(check)], env=env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)

    def test_resume_argv_and_foreground(self):
        argv = h.resume_argv('codex', 'session-123', ['-m', 'model', '--resume', 'other', 'prompt'])
        self.assertEqual(argv, ['codex', 'resume', 'session-123', '-m', 'model', '--no-daemon'])
        with self.assertRaises(ValueError):
            h.resume_argv('claude', "';rm -rf", [])
        self.assertEqual(h.foreground({'foreground': {'argv': ['python3', '/usr/local/bin/sleipnir-hermod', 'launch', 'id']}}), 'starting')

    def test_missing_cwd_blocks_without_launch(self):
        pane = self.start()
        self.db.execute("UPDATE pane SET subpath='gone'")
        self.db.commit()
        self.r.terminals = []
        self.r.phase1()
        self.assertEqual(h.panes(self.db)[0]['state'], 'blocked')
        self.assertNotIn(pane['id'], self.r.attempts)


if __name__ == '__main__':
    unittest.main()
