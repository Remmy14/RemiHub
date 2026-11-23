import contextlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from uuid import uuid4

SOURCE=Path(__file__).parents[1]/'deployments/agent_common/libexec/remihub-agent-card-cleanup'
loader=importlib.machinery.SourceFileLoader('card_cleanup',str(SOURCE));spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;loader.exec_module(m)

class CleanupTests(unittest.TestCase):
 def setUp(self):
  self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.repo=self.root/'repo.git';self.worktrees=self.root/'worktrees';self.worktrees.mkdir();(self.worktrees/'.locks').mkdir()
  self.state=self.root/'state';self.state.mkdir(mode=0o700)
  self.card=str(uuid4());self.branch='agent/card-'+self.card;self.path=self.worktrees/('card-'+self.card)
  self.user=pwd.getpwuid(os.getuid()).pw_name
  self.surface=m.Surface('test',self.user,'backend',self.repo,self.worktrees,True)
  self.cmd('git','init','--bare',str(self.repo));seed=self.root/'seed';self.cmd('git','clone',str(self.repo),str(seed))
  (seed/'source.txt').write_text('preserved');(seed/'.gitignore').write_text('build/\n.env\n')
  self.cmd('git','-C',str(seed),'-c','user.name=QA','-c','user.email=qa@example.invalid','add','.');self.cmd('git','-C',str(seed),'-c','user.name=QA','-c','user.email=qa@example.invalid','commit','-m','seed');self.cmd('git','-C',str(seed),'push','origin','HEAD:main')
  self.cmd('git','-C',str(self.repo),'worktree','add','-b',self.branch,str(self.path),'main')
  self.db={'cards':[{'id':self.card,'status':'closed','repository_scope':'backend','manual_close_exact':True,'closed_at':'2026-11-23T12:00:00+00:00','worktree_path':str(self.path)}],'runs':[]}
  self.policy={'enabled':True,'automatic_since':'2026-11-23T00:00:00+00:00','historical_cards':[],'hold_cards':[],'hold_paths':[],'max_resources':3}
  # Fixture state is owned by the invoking developer; live state still requires root.
  self.secpatch=patch.object(m,'secure_root_file',lambda p:m.no_symlinks(p));self.secpatch.start();self.addCleanup(self.secpatch.stop)
  self.gitpatch=patch.object(m,'git',self.git);self.gitpatch.start();self.addCleanup(self.gitpatch.stop)
 def cmd(self,*args):return subprocess.run(args,check=True,capture_output=True).stdout
 def git(self,surface,cwd,*args):return self.cmd('git','-c','core.fsmonitor=false','-C',str(cwd),*args)
 def sweep(self,dry=False,load=None,busy=None):return m.sweep(self.policy,dry_run=dry,surfaces=(self.surface,),load_db=load or (lambda:self.db),busy=busy or (lambda p:False),state_root=self.state)
 def retained(self):
  r=self.sweep();self.assertTrue(self.path.exists());self.assertEqual(r[0]['status'],'deferred');return r
 def test_manual_close_real_git_removal_preserves_branch_history_and_card(self):
  head=self.git(self.surface,self.repo,'rev-parse',self.branch)
  self.assertEqual(self.sweep()[0]['status'],'removed');self.assertFalse(self.path.exists())
  self.assertEqual(self.git(self.surface,self.repo,'rev-parse',self.branch),head);self.assertEqual(self.db['cards'][0]['status'],'closed')
  self.assertEqual(self.sweep(),[])
 def test_open_states_never_close_or_delete(self):
  for status in ('completed','cancelled','failed','implementing','reviewing','deploying'):
   self.db['cards'][0]['status']=status;self.retained();self.assertEqual(self.db['cards'][0]['status'],status)
 def test_missing_manual_event(self):self.db['cards'][0]['manual_close_exact']=False;self.retained()
 def test_active_and_expired_lease_states_retained(self):
  for status in m.ACTIVE:
   self.db['runs']=[{'card_id':self.card,'status':status,'lease_expires_at':'2000-01-01'}];self.retained()
 def test_dirty_tracked(self):(self.path/'source.txt').write_text('unpreserved');self.retained()
 def test_dirty_untracked(self):(self.path/'unpreserved.txt').write_text('new');self.retained()
 def test_unknown_ignored_secret_retained(self):(self.path/'.env').write_text('not a real secret');self.retained()
 def test_ignored_build_removed_with_registered_worktree(self):
  (self.path/'build').mkdir();(self.path/'build/output').write_text('disposable');self.assertEqual(self.sweep()[0]['status'],'removed')
 def test_process_in_use(self):
  r=self.sweep(busy=lambda p:True);self.assertEqual(r[0]['status'],'deferred');self.assertTrue(self.path.exists())
 def test_process_visibility_failure_defers(self):
  def unavailable(p):raise m.Deferred('process visibility incomplete')
  self.assertEqual(self.sweep(busy=unavailable)[0]['status'],'deferred');self.assertTrue(self.path.exists())
 def test_card_hold(self):self.policy['hold_cards']=[self.card];self.retained()
 def test_parent_hold(self):self.policy['hold_paths']=[str(self.worktrees)];self.retained()
 def test_cross_card_path_reference(self):self.db['cards'].append({**self.db['cards'][0],'id':str(uuid4())});self.retained()
 def test_wrong_persisted_path(self):self.db['cards'][0]['worktree_path']='/elsewhere';self.retained()
 def test_wrong_scope(self):self.db['cards'][0]['repository_scope']='android';self.retained()
 def test_symlink_target_is_preserved(self):
  target=self.root/'unrelated';self.path.rename(target);self.path.symlink_to(target,target_is_directory=True)
  self.retained();self.assertEqual((target/'source.txt').read_text(),'preserved')
 def test_symlink_parent_refused(self):
  target=self.root/'moved';self.worktrees.rename(target);self.worktrees.symlink_to(target,target_is_directory=True)
  result=self.sweep();self.assertEqual(result[0]['status'],'deferred');self.assertTrue((target/self.path.name).exists())
 def test_wrong_common_repository(self):
  other=self.root/'other.git';self.cmd('git','init','--bare',str(other));(self.path/'.git').write_text('gitdir: '+str(other)+'\n')
  self.assertNotEqual(self.sweep()[0]['status'],'removed');self.assertTrue(self.path.exists())
 def test_dry_run_never_removes_or_writes_state_or_locks(self):
  self.assertEqual(self.sweep(dry=True)[0]['status'],'eligible');self.assertTrue(self.path.exists());self.assertEqual(list(self.state.iterdir()),[]);self.assertEqual(list((self.worktrees/'.locks').iterdir()),[])
 def test_worker_lock_defers(self):
  import fcntl
  with (self.worktrees/'.locks'/(self.card+'.lock')).open('w') as f:
   fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);self.retained()
 def test_registration_locked(self):self.cmd('git','-C',str(self.repo),'worktree','lock',str(self.path));self.retained()
 def test_no_global_prune_for_missing_directory(self):
  import shutil
  shutil.rmtree(self.path);self.assertEqual(self.sweep()[0]['status'],'deferred');self.assertIn(str(self.path).encode(),self.git(self.surface,self.repo,'worktree','list','--porcelain'))
 def test_history_needs_explicit_allowlist(self):
  self.policy['automatic_since']='2026-11-24T00:00:00+00:00';self.retained();self.policy['historical_cards']=[self.card];self.assertEqual(self.sweep()[0]['status'],'removed')
 def test_closed_state_rechecked_under_lock(self):
  calls=0
  def db():
   nonlocal calls
   calls+=1
   if calls>1:self.db['cards'][0]['status']='completed'
   return self.db
  self.assertEqual(self.sweep(load=db)[0]['status'],'deferred');self.assertTrue(self.path.exists())
 def test_interruption_after_git_remove_recovers_intent(self):
  head=self.git(self.surface,self.repo,'rev-parse',self.branch).decode().strip()
  p=self.state/(self.card+'.json');m.atomic_json(p,{'resources':{str(self.path):{'status':'in_progress','head':head,'branch':'refs/heads/'+self.branch}}})
  self.git(self.surface,self.repo,'worktree','remove',str(self.path));m.atomic_json(self.state/'latest.json',{'resources':[]})
  m.recover_intents(self.state,(self.surface,));state=json.loads(p.read_text());self.assertEqual(state['resources'][str(self.path)]['status'],'completed')
 def test_removal_failure_keeps_card_closed_and_retries(self):
  actual=self.git
  def failed(surface,cwd,*args):
   if args[:2]==('worktree','remove'):raise OSError('synthetic removal failure')
   return actual(surface,cwd,*args)
  with patch.object(m,'git',failed):self.assertEqual(self.sweep()[0]['status'],'failed')
  self.assertTrue(self.path.exists());self.assertEqual(self.db['cards'][0]['status'],'closed')
  self.assertEqual(self.sweep()[0]['status'],'removed')
 def test_traversal_and_misleading_registration_refused(self):
  entry={'worktree':str(self.worktrees/'..'/'unrelated'),'branch':'refs/heads/'+self.branch}
  with self.assertRaises(m.Deferred):m.identity(self.surface,entry)
  entry={'worktree':str(self.path),'branch':'refs/heads/another-card'}
  with self.assertRaises(m.Deferred):m.identity(self.surface,entry)
 def test_sql_is_readonly_and_cleanup_cannot_close_cards(self):
  self.assertIn('BEGIN READ ONLY',m.SQL)
  self.assertNotRegex(m.SQL,r'(?i)\b(update|delete|insert|alter|drop)\b')
  self.assertNotIn('close_card(',SOURCE.read_text());self.assertNotIn('UPDATE agent.cards',SOURCE.read_text())

if __name__=='__main__':unittest.main()
