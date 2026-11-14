import copy
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
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

    def test_install_preserves_hook_order(self):
        path = self.path / 'hooks.json'
        path.write_text(json.dumps({'hooks': {'SessionStart': [
            {'matcher': 'startup', 'hooks': [{'type': 'command', 'command': 'user-first'}]},
            {'hooks': [{'command': 'user-second'}]}]}}))
        h.install_hooks(path, 'codex', [('SessionStart', 'start'), ('SessionEnd', 'end')])
        first = path.read_text()
        h.install_hooks(path, 'codex', [('SessionStart', 'start'), ('SessionEnd', 'end')])
        self.assertEqual(path.read_text(), first)
        hooks = json.loads(first)['hooks']
        self.assertEqual(hooks['SessionStart'][0]['hooks'][0]['command'], 'user-first')
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


    def test_snapshot_reads_full_terminal_records(self):
        with patch.object(h, 'core_rpc', return_value={'result': {'terminals': [self.term]}}) as rpc:
            self.assertEqual(h.snapshot(), [self.term])
        rpc.assert_called_once_with('terminal.list', {'all': True}, timeout=15)

    def test_recreated_terminal_gets_created_ms_and_is_not_recreated(self):
        self.start()
        self.r.terminals = []
        self.r.app_tabs = set()
        self.r.core = lambda *args, **kwargs: json.dumps({'address': 't9', 'state': 'live'})
        full = dict(self.term, address='t9', created_ms=999,
                    labels={'herdr.space': 's1', 'herdr.tab': 'tab9', 'where': 'Project / Work'})
        with patch.object(h, 'terminal_record', return_value=full):
            self.r.phase1()
        pane = h.panes(self.db)[0]
        self.assertEqual((pane['herdr_terminal_id'], pane['herdr_terminal_created_ms']), ('t9', 999))
        calls = []
        self.r.core = lambda *args, **kwargs: calls.append(args)
        self.r.phase1()
        self.assertEqual(calls, [])

    def test_pane_without_created_ms_is_adopted_and_closes(self):
        self.start()
        self.db.execute('UPDATE pane SET herdr_terminal_created_ms=NULL')
        self.db.commit()
        self.r.refresh([self.term])
        self.assertEqual(h.panes(self.db)[0]['herdr_terminal_created_ms'], 123)
        self.db.execute('UPDATE pane SET herdr_terminal_created_ms=NULL')
        self.db.commit()
        closed = dict(self.term, state='finished', reason='killed')
        self.r.refresh([closed])
        self.assertEqual(h.panes(self.db)[0]['state'], 'closed')
        calls = []
        self.r.core = lambda *args, **kwargs: calls.append(args)
        self.r.phase1()
        self.assertEqual(calls, [])

    def test_workspace_name_comes_from_the_first_app_space(self):
        self.term = dict(self.term, labels={'shepherd.space-name': 'Requested'})
        self.r.terminals = [self.term]
        self.start()
        self.assertEqual(h.panes(self.db)[0]['workspace_name'], 'Requested')
        placed = dict(self.term, labels={'herdr.space': 's1', 'herdr.tab': 'tab1', 'where': 'App Space / Work'})
        self.r.refresh([placed])
        self.assertEqual(h.panes(self.db)[0]['workspace_name'], 'App Space')
        later = dict(self.term, labels={'herdr.space': 's1', 'herdr.tab': 'tab2', 'where': 'Later / Work'})
        self.r.refresh([later])
        self.assertEqual(h.panes(self.db)[0]['workspace_name'], 'App Space')

    def test_install_sets_three_second_hook_timeouts(self):
        path = self.path / 'hooks.json'
        path.write_text(json.dumps({'hooks': {'SessionEnd': [{'hooks': [
            {'type': 'command', 'command': 'sleipnir-hermod hook codex end', 'timeout': 10}]}]}}))
        h.install_hooks(path, 'codex', [('SessionStart', 'start'), ('SessionEnd', 'end')])
        hooks = json.loads(path.read_text())['hooks']
        self.assertEqual(len(hooks['SessionEnd']), 1)
        self.assertEqual([hook['timeout'] for name in ('SessionStart', 'SessionEnd')
                          for group in hooks[name] for hook in group['hooks']], [3, 3])


    def launcher_tree(self, child_args):
        launcher = {'name': 'python3', 'pid': 100,
                    'argv': ['python3', '/usr/local/bin/sleipnir-hermod', 'launch', 'pane']}
        children = {'100': ['200'] if child_args else []}
        args = {'200': child_args}
        return launcher, (patch.object(h, 'proc_children', side_effect=lambda pid: children.get(str(pid), [])),
                          patch.object(h, 'proc_args', side_effect=lambda pid: args.get(str(pid), [])))

    def test_launcher_with_agent_child_counts_as_running(self):
        launcher, (children, args) = self.launcher_tree(['claude', '--resume', 'session-123'])
        with children, args:
            self.assertEqual(h.foreground({'foreground': launcher}), 'running')
        launcher, (children, args) = self.launcher_tree([])
        with children, args:
            self.assertEqual(h.foreground({'foreground': launcher}), 'starting')

    def test_running_agent_under_launcher_is_not_retried(self):
        pane = self.start()
        with self.db:
            h.record(self.db, 'launch', pane['id'])
        self.r.apply_inbox(dict(self.event, at=h.now() + 100), 'ack.json')
        launcher, (children, args) = self.launcher_tree(['claude', '--resume', 'session-123'])
        self.term['foreground'] = launcher
        calls = []
        self.r.core = lambda *a, **k: calls.append(a) or ''
        with children, args, patch.object(h, 'session_held', return_value=True):
            for _ in range(3):
                self.r.phase2()
                for attempt in self.r.attempts.values():
                    attempt['deadline'] = 0
        self.assertNotIn(pane['id'], self.r.attempts)
        self.assertEqual(self.r.runtime[pane['id']], 'running')
        self.assertEqual(calls, [])
        failed = self.db.execute("SELECT count(*) FROM event WHERE kind='failed'").fetchone()[0]
        self.assertEqual(failed, 0)

    def test_stale_claude_registry_with_reused_pid_is_not_held(self):
        sessions = self.path / 'home/.claude/sessions'
        sessions.mkdir(parents=True)
        (sessions / '321.json').write_text(json.dumps({'pid': 321, 'sessionId': 'session-123',
                                                       'procStart': '5000'}))
        with patch.object(h.Path, 'home', return_value=self.path / 'home'):
            with patch.object(h, 'proc_args', return_value=['zsh']), \
                    patch.object(h, 'proc_start', return_value='5000'):
                self.assertFalse(h.session_held('claude', 'session-123'))
            with patch.object(h, 'proc_args', return_value=['claude', '--resume']), \
                    patch.object(h, 'proc_start', return_value='9999'):
                self.assertFalse(h.session_held('claude', 'session-123'))
            with patch.object(h, 'proc_args', return_value=['claude', '--resume']), \
                    patch.object(h, 'proc_start', return_value='5000'):
                self.assertTrue(h.session_held('claude', 'session-123'))


    def test_prune_removes_old_closed_panes_and_leftovers(self):
        pane = self.start()
        old = h.now() - h.RETENTION_MS - 1000
        with self.db:
            h.record(self.db, 'old', pane['id'], at=old)
            h.record(self.db, 'recent', pane['id'])
        h.prune(self.db, self.path / 'state')
        self.assertEqual([r['kind'] for r in self.db.execute('SELECT kind FROM event WHERE pane_id=?', (pane['id'],))
                          if r['kind'] in ('old', 'recent')], ['recent'])
        self.assertEqual(len(h.panes(self.db)), 1)
        with self.db:
            self.db.execute("UPDATE pane SET state='closed',updated_at=?", (old,))
        inbox = self.path / 'state/inbox'
        inbox.mkdir(parents=True, exist_ok=True)
        stale, fresh = inbox / '.tmp-stale', inbox / '.tmp-fresh'
        stale.write_text('x')
        fresh.write_text('x')
        os.utime(stale, (time.time() - 2 * 86400,) * 2)
        h.prune(self.db, self.path / 'state')
        for table in ('pane', 'tab', 'workspace'):
            self.assertEqual(self.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM event WHERE pane_id IS NOT NULL').fetchone()[0], 0)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_recent_closed_pane_is_kept(self):
        self.start()
        with self.db:
            self.db.execute("UPDATE pane SET state='closed',updated_at=?", (h.now(),))
        h.prune(self.db, self.path / 'state')
        self.assertEqual(len(h.panes(self.db)), 1)

    def test_start_queued_before_current_shell_keeps_pane_eligible(self):
        pane = self.start()
        self.r.eligible.add(pane['id'])
        with patch.object(h, 'program_started_ms', return_value=h.now() + 60000):
            self.r.apply_inbox(dict(self.event, at=h.now()), 'queued.json')
        self.assertIn(pane['id'], self.r.eligible)
        with patch.object(h, 'program_started_ms', return_value=0):
            self.r.apply_inbox(dict(self.event, at=h.now() + 1000), 'fresh.json')
        self.assertNotIn(pane['id'], self.r.eligible)


    def test_launch_flags_keep_resume_flags_only(self):
        args = ['--model', 'opus', '--add-dir', 'a', 'b', '-p', 'prompt', '--resume', 'old', '--verbose',
                '--verbose=yes', '--settings=s.json', '--', '--model', 'later']
        self.assertEqual(h.launch_flags('claude', args),
                         [['--model', 'opus'], ['--add-dir', 'a', 'b'], ['--verbose'], ['--settings=s.json']])
        self.assertEqual(h.launch_flags('codex', ['-m', 'gpt', 'resume', 'id', '--no-daemon']),
                         [['-m', 'gpt'], ['--no-daemon']])
        self.assertEqual(h.launch_flags('claude', ['--model']), [])

    def test_prompt_actions(self):
        self.assertEqual(h.prompt_action('Update available 1 → 2\n› 1. Update now\n2. Skip'), 'update')
        self.assertEqual(h.prompt_action('Hooks need review\n2. Trust all and continue'), 'trust-hooks')
        self.assertEqual(h.prompt_action('Do you trust this folder?'), 'user')
        self.assertIsNone(h.prompt_action('› Ask Codex to do anything'))

    def test_update_prompt_is_accepted_and_agent_restarts(self):
        pane = self.start()
        self.r.attempts[pane['id']] = {'terminal': h.incarnation(self.term), 'session': 'session-123', 'deadline': 0}
        calls = []
        self.r.core = lambda *a, **k: calls.append(a) or ('Update available\n› 1. Update now' if a[0] == 'read' else '')
        self.r.phase2()
        self.assertIn(('keys', 't1', 'enter'), calls)
        self.assertIn(pane['id'], self.r.updating)
        self.r.apply_inbox(dict(kind='launch-exit', terminal='t1', created_ms=123, session_id='session-123',
                                returncode=0, at=h.now()), 'updated.json')
        self.assertEqual(h.panes(self.db)[0]['state'], 'open')
        self.assertIn(pane['id'], self.r.eligible)
        self.assertNotIn(pane['id'], self.r.updating)

    def test_judged_launch_is_not_judged_again(self):
        pane = self.start()
        with self.db:
            h.record(self.db, 'launch', pane['id'])
            h.record(self.db, 'failed', pane['id'])
        launcher, (children, args) = self.launcher_tree([])
        self.term['foreground'] = launcher
        with children, args:
            self.r.phase2()
        self.assertNotIn(pane['id'], self.r.attempts)

    def test_codex_session_start_hook_matches_resume(self):
        path = self.path / 'codex-hooks.json'
        path.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [
            {'type': 'command', 'command': 'sleipnir-hermod hook codex start', 'timeout': 3}]}]}}))
        h.install_hooks(path, 'codex', [('SessionStart', 'start', 'startup|resume|clear|compact'), ('SessionEnd', 'end')])
        hooks = json.loads(path.read_text())['hooks']
        self.assertEqual(len(hooks['SessionStart']), 1)
        self.assertEqual(hooks['SessionStart'][0]['matcher'], 'startup|resume|clear|compact')
        self.assertNotIn('matcher', hooks['SessionEnd'][0])


if __name__ == '__main__':
    unittest.main()
