"""Real-Git tests for the proposed fixed-path helper's checkout primitive."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

path=Path(__file__).resolve().parents[1]/'deployments/agent_backend/libexec/remihub-backend-deployment-control'
from importlib.machinery import SourceFileLoader
loader=SourceFileLoader('proposed_control',str(path))
spec=importlib.util.spec_from_loader(loader.name,loader)
control=importlib.util.module_from_spec(spec);sys.modules[spec.name]=control;spec.loader.exec_module(control)

class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=Path(self.tmp.name)/'repo';self.repo.mkdir()
        self.commands=[]
        self.old_run=control.run;control.run=self.run_command;self.addCleanup(setattr,control,'run',self.old_run)
        self.old_config=control.GIT_SAFE_CONFIG;control.GIT_SAFE_CONFIG=Path('/dev/null');self.addCleanup(setattr,control,'GIT_SAFE_CONFIG',self.old_config)
        self.git('init','-q','-b','main');self.git('config','user.name','Fixture');self.git('config','user.email','fixture@example.invalid')
        (self.repo/'.gitignore').write_text('ignored.txt\n')
        (self.repo/'backend').mkdir();(self.repo/'backend/agent_worker.py').write_text('VALUE = True\n')
        (self.repo/'existing.txt').write_text('base\n');self.git('add','.');self.git('commit','-qm','base')
        self.base=self.git('rev-parse','HEAD')
        (self.repo/'existing.txt').write_text('candidate\n');(self.repo/'new.txt').write_text('new\n')
        self.git('add','.');self.git('commit','-qm','candidate');self.candidate=self.git('rev-parse','HEAD')
        self.git('branch','candidate',self.candidate)
        self.git('switch','--detach',self.base);self.git('update-ref','refs/heads/main',self.base,self.candidate);self.git('symbolic-ref','HEAD','refs/heads/main')

    def run_command(self,command,*,user=None,check=True):
        self.commands.append(command)
        self.assertNotIn('--hard',command);self.assertNotIn('--force',command);self.assertNotIn('--discard-changes',command)
        if command[0] == 'git':command = [control.GIT, *command[1:]]
        result=self.old_run(command,user=None,check=False)
        if check and result.returncode:raise SystemExit(result.stderr)
        return result

    def git(self,*args):return self.run_command(['git','-C',str(self.repo),*args]).stdout.strip()
    def move(self,expected,target):control.move_clean_checkout(self.repo,'main',expected,target,user='fixture')

    def test_forward_then_rollback_retains_both_commits_and_unrelated_files(self):
        (self.repo/'untracked.txt').write_text('preserve');(self.repo/'ignored.txt').write_text('preserve ignored')
        self.move(self.base,self.candidate);self.assertEqual(self.git('rev-parse','HEAD'),self.candidate)
        self.move(self.candidate,self.base);self.assertEqual(self.git('rev-parse','HEAD'),self.base)
        self.assertEqual(self.git('symbolic-ref','--short','HEAD'),'main')
        self.assertEqual((self.repo/'untracked.txt').read_text(),'preserve')
        self.assertEqual((self.repo/'ignored.txt').read_text(),'preserve ignored')
        for oid in [self.base,self.candidate]:self.assertEqual(self.git('rev-parse','refs/remihub-retained/'+oid),oid)

    def test_dirty_tracked_source_is_preserved_and_rejected(self):
        (self.repo/'existing.txt').write_text('unrelated user edit')
        with self.assertRaises(SystemExit):self.move(self.base,self.candidate)
        self.assertEqual((self.repo/'existing.txt').read_text(),'unrelated user edit')
        self.assertEqual(self.git('rev-parse','HEAD'),self.base)

    def test_untracked_collision_is_preserved_and_rejected(self):
        (self.repo/'new.txt').write_text('user data')
        with self.assertRaises(SystemExit):self.move(self.base,self.candidate)
        self.assertEqual((self.repo/'new.txt').read_text(),'user data')
        self.assertEqual(self.git('rev-parse','HEAD'),self.base)

    def test_ignored_collision_is_preserved_and_rejected(self):
        self.git('switch','--detach',self.candidate)
        (self.repo/'ignored.txt').write_text('tracked candidate');self.git('add','-f','ignored.txt');self.git('commit','-qm','ignored becomes tracked');target=self.git('rev-parse','HEAD')
        self.git('switch','main');(self.repo/'ignored.txt').write_text('private local file')
        with self.assertRaises(SystemExit):self.move(self.base,target)
        self.assertEqual((self.repo/'ignored.txt').read_text(),'private local file')
        self.assertEqual(self.git('rev-parse','HEAD'),self.base)

    def test_ignored_collision_during_rollback_is_preserved(self):
        self.git('switch', '--detach', self.base)
        (self.repo/'ignored.txt').write_text('approved tracked file')
        self.git('add', '-f', 'ignored.txt');self.git('commit', '-qm', 'old tracked file')
        before = self.git('rev-parse', 'HEAD')
        self.git('rm', 'ignored.txt');self.git('commit', '-qm', 'remove tracked file')
        after = self.git('rev-parse', 'HEAD')
        self.git('switch', '--detach', before)
        self.git('update-ref', 'refs/heads/main', before, self.base)
        self.git('symbolic-ref', 'HEAD', 'refs/heads/main')
        self.move(before, after)
        (self.repo/'ignored.txt').write_text('private local replacement')
        with self.assertRaises(SystemExit):self.move(after, before)
        self.assertEqual((self.repo/'ignored.txt').read_text(), 'private local replacement')
        self.assertEqual(self.git('rev-parse', 'HEAD'), after)

    def test_configured_filters_and_candidate_attributes_cannot_execute(self):
        self.git('switch', '--detach', self.candidate)
        (self.repo/'.gitattributes').write_text('*.txt filter=fixture\n')
        self.git('add', '.');self.git('commit', '-qm', 'candidate filter attributes')
        target = self.git('rev-parse', 'HEAD');self.git('switch', 'main')
        marker = self.repo.parent/'filter-executed'
        for kind in ['smudge', 'clean', 'process']:
            self.git('config', 'filter.fixture.' + kind, 'touch ' + str(marker))
        self.git('config', 'filter.fixture.required', 'true')
        self.move(self.base, target)
        self.assertFalse(marker.exists())
        self.assertEqual((self.repo/'existing.txt').read_text(), 'candidate\n')
        self.move(target, self.base)
        self.assertFalse(marker.exists())
        self.assertEqual((self.repo/'existing.txt').read_text(), 'base\n')

    def synchronization_fixture(self, candidate):
        source=self.repo.parent/'implementation.git';target=self.repo.parent/'production.git';runtime=self.repo.parent/'runtime'
        for path in [source, target]:self.git('clone', '--bare', str(self.repo), str(path))
        self.git('clone', str(self.repo), str(runtime))
        def git_at(path,*args):return self.run_command([control.GIT,'-C',str(path),*args]).stdout.strip()
        git_at(runtime,'switch','--detach',candidate)
        git_at(runtime,'update-ref','refs/heads/main',candidate,self.base)
        git_at(runtime,'symbolic-ref','HEAD','refs/heads/main')
        branch='deployment/card-11111111-1111-4111-8111-111111111111/r1'
        git_at(target,'update-ref','refs/heads/'+branch,candidate)
        git_at(target,'update-ref','refs/heads/production-main',candidate)
        environment=control.Environment(service='fixture',target_repo=target,target_branch='production-main',
            runtime=runtime,runtime_branch='main',runtime_user='root',frontend_backup_root=self.repo.parent/'backups')
        return source, environment, branch, git_at

    def test_bare_source_promotion_and_rollback_retain_both_commits(self):
        source,environment,branch,git_at=self.synchronization_fixture(self.candidate)
        with patch.object(control,'SOURCE_REPO',source), patch.object(control,'PLANNING_REPO',self.repo), \
             patch.dict(control.ENVIRONMENTS,{'production':environment}), patch.object(control,'harden_planning_checkout'):
            control.synchronize_sources(environment,[branch,self.candidate,self.base,
                'rollback-before-agent-card-11111111-1111-4111-8111-111111111111-r1'])
            self.assertEqual(git_at(source,'rev-parse','refs/heads/main'),self.candidate)
            for oid in [self.base,self.candidate]:self.assertEqual(git_at(source,'rev-parse','refs/remihub-retained/'+oid),oid)
            control.restore_sources(environment,[self.candidate,self.base])
            self.assertEqual(git_at(source,'rev-parse','refs/heads/main'),self.base)
            self.assertEqual(self.git('rev-parse','HEAD'),self.base)
            for oid in [self.base,self.candidate]:self.assertEqual(git_at(source,'rev-parse','refs/remihub-retained/'+oid),oid)

    def test_bare_source_rejects_non_fast_forward_without_rewriting_sources(self):
        self.git('switch','--orphan','unrelated')
        (self.repo/'orphan.txt').write_text('unrelated source')
        self.git('add','.');self.git('commit','-qm','unrelated source root')
        unrelated=self.git('rev-parse','HEAD');self.git('switch','main')
        source,environment,branch,git_at=self.synchronization_fixture(unrelated)
        with patch.object(control,'SOURCE_REPO',source), patch.object(control,'PLANNING_REPO',self.repo), \
             patch.dict(control.ENVIRONMENTS,{'production':environment}), patch.object(control,'harden_planning_checkout'):
            with self.assertRaises(SystemExit):control.synchronize_sources(environment,[branch,unrelated,self.base,
                'rollback-before-agent-card-11111111-1111-4111-8111-111111111111-r1'])
        self.assertEqual(git_at(source,'rev-parse','refs/heads/main'),self.base)
        self.assertEqual(self.git('rev-parse','HEAD'),self.base)

    def test_wrong_expected_commit_and_branch_fail_closed(self):
        with self.assertRaises(SystemExit):self.move(self.candidate,self.base)
        self.git('switch','-c','other')
        with self.assertRaises(SystemExit):self.move(self.base,self.candidate)
        self.assertEqual(self.git('rev-parse','HEAD'),self.base)

    def test_diverged_history_is_rejected(self):
        (self.repo/'other.txt').write_text('other');self.git('add','.');self.git('commit','-qm','diverged');other=self.git('rev-parse','HEAD')
        with self.assertRaises(SystemExit):self.move(other,self.candidate)
        self.assertEqual(self.git('rev-parse','HEAD'),other)

    def test_rollback_preserves_local_changes_and_fails(self):
        self.move(self.base,self.candidate);(self.repo/'new.txt').write_text('post-release user edit')
        with self.assertRaises(SystemExit):self.move(self.candidate,self.base)
        self.assertEqual(self.git('rev-parse','HEAD'),self.candidate)
        self.assertEqual((self.repo/'new.txt').read_text(),'post-release user edit')

if __name__=='__main__':unittest.main(verbosity=2)
