"""Real frozen Git candidates; deterministic reviewer is a workflow fixture, not an approval."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.core.agent_state import CardStatus, RepositoryScope, RunPhase
from backend.core.codex_review import (CodexReviewExecutor, build_patch, candidate_digest,
    parse_verdict, review_target, require_approval)
from backend.core.review_source import SourceSnapshot, SourceInspector, changed_manifest, MAX_RESULT
from backend.services.agent_service import _allowed_actions
from tests.test_agent_worker import claimed_run


class InspectingReviewer:
    """Uses actual frozen source to detect a deliberately missing refresh call."""
    def prepare(self): pass
    def cancel(self): pass
    def run_turn(self, *, repository_path, prompt, model, inspector):
        context = json.loads(prompt)
        assert context['changed_files'][0]['file'] == 'frontend-web/Dashboard.tsx'
        contract = inspector.inspect(operation='search', query='get_usage', repository='candidate')
        assert any(h['file'] == 'backend/api.py' for h in contract['matches'])
        text = inspector.inspect(operation='read', path='frontend-web/Dashboard.tsx')['text']
        inspector.inspect(operation='diff')
        defective = 'refreshSummary()' not in text
        response = {'decision': 'request_changes' if defective else 'approve',
            'summary': 'Refresh is missing' if defective else 'Fixture meets requirements',
            'findings': [{'file': 'frontend-web/Dashboard.tsx', 'issue': 'Card update does not refresh summary',
                          'required_change': 'Refresh summary on card changes'}] if defective else []}
        return {'response': json.dumps(response), 'thread_id': 'independent-thread', 'turn_id': str(uuid4()),
                'actual_model': 'synthetic-fixture', 'cli_version': 'fixture'}


class ReviewCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name);self.repo = self.root/'repo';self.repo.mkdir()
        self.artifacts = self.root/'artifacts';self.artifacts.mkdir()
        self.git('init', '-q');self.git('config', 'user.name', 'Fixture');self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo/'backend').mkdir();(self.repo/'frontend-web').mkdir()
        (self.repo/'backend/api.py').write_text('def get_usage(card_id):\n    return {"card_id":card_id, "total":42}\n')
        (self.repo/'frontend-web/Dashboard.tsx').write_text('function updateCard() { oldBehavior(); }\n')
        self.git('add','.');self.git('commit','-qm','base');self.base=self.git('rev-parse','HEAD').strip()
        self.gateway=InspectingReviewer()
        self.writer=patch('backend.services.agent_usage_writer.record_turn');self.writer.start();self.addCleanup(self.writer.stop)

    def git(self,*args):
        return subprocess.run(['git','-C',str(self.repo),*args],check=True,capture_output=True,text=True).stdout

    def claim(self, *, fixed=True, attempt=1, scope=RepositoryScope.BACKEND):
        (self.repo/'frontend-web/Dashboard.tsx').write_text('function updateCard() { '+('refreshSummary();' if fixed else 'loadDetails();')+' }\n')
        impl=str(uuid4());patch_bytes=build_patch(self.repo);artifact=self.artifacts/(impl+'.patch');artifact.write_bytes(patch_bytes)
        metadata={'repository_scope':scope.value,'summary':'Updated dashboard',
            'tests':[], 'trusted_validation':{'success':True,'checks':[{'status':'passed'}]},
            'workspace':{'artifact_patch':str(artifact),'base_commit':self.base,'worktree_path':str(self.repo),
                         'patch_sha256':hashlib.sha256(patch_bytes).hexdigest()}}
        metadata['candidate_digest']=candidate_digest(metadata,run_id=impl,revision=1)
        return replace(claimed_run(phase=RunPhase.IMPLEMENTATION),id=str(uuid4()),phase=RunPhase.REVIEW,
            card_status=CardStatus.REVIEWING,repository_scope=scope,
            result_metadata={'review_source':{'review_cycle':str(uuid4()),'review_attempt':attempt,
                'correction_allowance':3,'implementation_run_id':impl,'implementation_metadata':metadata}})

    def executor(self,scope=RepositoryScope.BACKEND):
        return CodexReviewExecutor(artifact_root=self.artifacts,repository_scope=scope,gateway=self.gateway,model='configured-model-v2')

    def test_approval_reads_actual_unchanged_python_contract_for_typescript(self):
        result=self.executor().execute(self.claim())
        self.assertEqual(result.card_status,CardStatus.REVIEW_READY)
        evidence=result.metadata['independent_review'];self.assertEqual(evidence['decision'],'approve')
        self.assertEqual(evidence['source_manifest'][0]['file'],'frontend-web/Dashboard.tsx')
        self.assertNotIn('verdict',evidence);require_approval(evidence)

    def test_real_defect_fix_and_repeated_correction_budget(self):
        for attempt in range(1,5):
            result=self.executor().execute(self.claim(fixed=False,attempt=attempt))
            expected=CardStatus.IMPLEMENTATION_QUEUED if attempt<=3 else CardStatus.REVIEW_CORRECTIONS_PAUSED
            self.assertEqual(result.card_status,expected)
            self.assertEqual(result.metadata['independent_review']['decision'],'request_changes')
        self.assertEqual(self.executor().execute(self.claim(fixed=True,attempt=5)).card_status,CardStatus.REVIEW_READY)

    def test_operational_timeout_and_malformed_output_do_not_become_decisions(self):
        class Broken(InspectingReviewer):
            def run_turn(self,**kw):raise TimeoutError('fixture interruption')
        self.gateway=Broken()
        with self.assertRaises(TimeoutError):self.executor().execute(self.claim(fixed=False))
        class Malformed(InspectingReviewer):
            def run_turn(self,**kw):return {'response':'{"decision":"blocked"}','thread_id':'r','turn_id':'t'}
        self.gateway=Malformed()
        with self.assertRaises(ValueError):self.executor().execute(self.claim())

    def test_failed_execution_retains_observed_model_and_reasoning_for_completed_turns(self):
        class Broken(InspectingReviewer):
            def run_turn(self, **kw):
                error=RuntimeError('fixture upstream failure')
                error.codex_turn_id='failed-turn';error.codex_usage=None
                error.codex_reasoning_effort='high'
                error.codex_turns=[{'turn_id':'completed-turn','usage':{'last':{'total_tokens':12}},'actual_model':'observed-model'}]
                raise error
        self.gateway=Broken()
        with patch('backend.services.agent_usage_writer.record_turn') as writer:
            with self.assertRaises(RuntimeError):self.executor().execute(self.claim())
        rows={call.kwargs['turn_id']:call.kwargs for call in writer.call_args_list}
        self.assertEqual(rows['completed-turn']['actual_model'],'observed-model')
        self.assertEqual(rows['completed-turn']['reasoning_effort'],'high')
        self.assertIsNone(rows['failed-turn']['usage'])
        self.assertIsNone(rows['failed-turn']['actual_model'])

    def test_kotlin_candidate_can_inspect_authorized_backend_snapshot(self):
        support=self.root/'backend-support'
        subprocess.run(['git','clone','-q',str(self.repo),str(support)],check=True,capture_output=True)
        self.git('rm','backend/api.py')
        (self.repo/'android').mkdir();(self.repo/'android/Client.kt').write_text('val total = 0\n')
        self.git('add','.');self.git('commit','-qm','android base');self.base=self.git('rev-parse','HEAD').strip()
        claim=self.claim(scope=RepositoryScope.ANDROID)
        (self.repo/'android/Client.kt').write_text('val total = usage.total\n')
        source=claim.result_metadata['review_source'];m=source['implementation_metadata'];raw=build_patch(self.repo)
        Path(m['workspace']['artifact_patch']).write_bytes(raw);m['workspace']['patch_sha256']=hashlib.sha256(raw).hexdigest()
        m['candidate_digest']=candidate_digest(m,run_id=source['implementation_run_id'],revision=1)
        outer=self
        class KotlinReviewer(InspectingReviewer):
            def run_turn(self,*,repository_path,prompt,model,inspector):
                matches=inspector.inspect(operation='search',repository='backend',query='get_usage')['matches']
                outer.assertEqual(matches[0]['file'],'backend/api.py')
                outer.assertIn('usage.total',inspector.inspect(operation='read',path='android/Client.kt')['text'])
                inspector.inspect(operation='diff')
                return {'response':json.dumps({'decision':'approve','summary':'Kotlin inspected Python contract','findings':[]}),
                        'thread_id':'kotlin-review','turn_id':'kotlin-turn'}
        executor=CodexReviewExecutor(artifact_root=self.artifacts,repository_scope=RepositoryScope.ANDROID,
            supporting_repositories={RepositoryScope.BACKEND:support},gateway=KotlinReviewer(),model='fixture')
        result=executor.execute(claim)
        self.assertEqual(result.card_status,CardStatus.REVIEW_READY)
        self.assertEqual(result.metadata['independent_review']['supporting_references']['backend'],self.git_support_head(support))

    def git_support_head(self,path):
        return subprocess.run(['git','-C',str(path),'rev-parse','HEAD'],check=True,capture_output=True,text=True).stdout.strip()

    def test_mutation_of_live_candidate_is_detected(self):
        claim=self.claim();(self.repo/'backend/api.py').write_text('changed during review\n')
        with self.assertRaises(ValueError):self.executor().execute(claim)

    def test_validator_failure_is_supplied_truthfully_without_parser_command_gates(self):
        claim=self.claim();source=claim.result_metadata['review_source'];m=source['implementation_metadata']
        m['trusted_validation']={'success':False,'error':'validator unavailable'}
        m['candidate_digest']=candidate_digest(m,run_id=source['implementation_run_id'],revision=1)
        result=self.executor().execute(claim)
        self.assertFalse(result.metadata['independent_review']['validation_evidence']['trusted']['success'])
        # Independent code review does not manufacture validation success; deployer still validates.

    def test_cancellation_is_not_cleared_by_prepare(self):
        claim=self.claim();executor=self.executor();executor.cancel(claim)
        with self.assertRaises(TimeoutError):executor.execute(claim)

    def test_export_attributes_cannot_hide_or_rewrite_supporting_sources(self):
        from backend.core.codex_review import _freeze_base
        (self.repo/'.gitattributes').write_text('backend/api.py export-ignore\n*.txt export-subst\n')
        (self.repo/'revision.txt').write_text('$Format:%H$\n')
        self.git('add','.');self.git('commit','-qm','export-attributes')
        self.base=self.git('rev-parse','HEAD').strip()
        target=self.root/'export';target.mkdir()
        manifest=_freeze_base(self.repo,self.base,target)
        self.assertIn('backend/api.py',manifest)
        self.assertEqual((target/'revision.txt').read_text(),'$Format:%H$\n')

    def test_binary_untracked_source_and_modes_are_preserved(self):
        from backend.core.codex_review import _freeze_base
        claim=self.claim();(self.repo/'asset.bin').write_bytes(b'\0\xff\x01')
        (self.repo/'new.sh').write_text('#!/bin/sh\ntrue\n');(self.repo/'new.sh').chmod(0o755)
        self.assertIn(b'GIT binary patch',build_patch(self.repo))
        target=self.root/'export';target.mkdir();base=_freeze_base(self.repo,self.base,target)
        subprocess.run(['git','-C',str(target),'apply','--no-index','--binary','-'],input=build_patch(self.repo),check=True,capture_output=True)
        snapshot=SourceSnapshot(target);manifest=changed_manifest(base,snapshot)
        self.assertEqual(snapshot.files['asset.bin'][0],b'\0\xff\x01')
        self.assertEqual(snapshot.files['new.sh'][1],0o100755)
        self.assertIn('asset.bin',{entry['file'] for entry in manifest})

    def test_artifact_escape_and_stale_digest_are_execution_failures(self):
        claim=self.claim();source=claim.result_metadata['review_source'];metadata=source['implementation_metadata']
        outside=self.root/'outside.patch';outside.write_bytes(Path(metadata['workspace']['artifact_patch']).read_bytes())
        metadata['workspace']['artifact_patch']=str(outside)
        with self.assertRaises(ValueError):self.executor().execute(claim)
        claim=self.claim();claim.result_metadata['review_source']['implementation_metadata']['candidate_digest']='0'*64
        with self.assertRaises(ValueError):self.executor().execute(claim)


class SourceSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'api.py').write_text('secret-free API contract\n')
        (self.root/'Client.kt').write_text('val contract = "API contract"\n')
        (self.root/'.env').write_text('PRIVATE_SENTINEL')
        (self.root/'large.txt').write_text('a'* (MAX_RESULT*3+1))
        self.snapshot=SourceSnapshot(self.root)
        self.inspector=SourceInspector({'candidate':self.snapshot,'backend':self.snapshot},b'x'*(MAX_RESULT*3+3))

    def test_source_is_immutable_and_cross_language_search_is_unrestricted(self):
        (self.root/'api.py').write_text('mutated')
        self.assertIn('API contract',self.inspector.inspect(operation='read',path='api.py')['text'])
        hits=self.inspector.inspect(operation='search',repository='backend',query='API contract')['matches']
        self.assertEqual({x['file'] for x in hits},{'api.py','Client.kt'})
        self.assertNotIn('.env',self.inspector.inspect(operation='list')['entries'])

    def test_traversal_symlinks_credentials_writes_and_unknown_repository_denied(self):
        for args in [{'operation':'read','path':'../etc/passwd'},{'operation':'read','path':'/etc/passwd'},
                     {'operation':'read','path':'.env'},{'operation':'write','path':'api.py'},
                     {'operation':'read','path':'api.py','repository':'host'}]:
            with self.subTest(args=args),self.assertRaises(ValueError):self.inspector.inspect(**args)
        (self.root/'escape').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):SourceSnapshot(self.root)

    def test_credentials_are_absent_from_candidate_base_and_supporting_source(self):
        names = ['.npmrc', 'nested/.netrc', '.pypirc', '.git-credentials',
                 '.docker/config.json', '.kube/config', 'nested/.NPMRC', 'key.properties',
                 'gradle.properties', 'local.properties', 'app/google-services.json']
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('CREDENTIAL_SENTINEL')
        snapshot = SourceSnapshot(self.root)
        inspector = SourceInspector({name: snapshot for name in ['candidate', 'base', 'backend', 'android']}, b'')
        for repository in inspector.snapshots:
            self.assertEqual(inspector.inspect(operation='search', repository=repository,
                             query='CREDENTIAL_SENTINEL')['matches'], [])
            for name in names:
                with self.subTest(repository=repository, path=name), self.assertRaises(ValueError):
                    inspector.inspect(operation='read', repository=repository, path=name)
                self.assertNotIn(name, snapshot.files)
        # Credential-only changes cannot leak through the verified diff either.
        before = {name: dict(value) for name, value in snapshot.manifest.items()}
        (self.root/'.npmrc').write_text('NEW_CREDENTIAL_SENTINEL')
        with self.assertRaisesRegex(ValueError, 'Changed protected source'):
            changed_manifest(before, SourceSnapshot(self.root))

    def test_chunked_source_and_diff_have_no_silent_omission(self):
        for operation in ['read','diff']:
            offset,text=0,''
            while offset is not None:
                result=self.inspector.inspect(operation=operation,path='large.txt',offset=offset)
                self.assertLessEqual(len(result['text']),MAX_RESULT)
                text+=result['text'];offset=result['next_offset']
            self.assertEqual(text, 'a'*(MAX_RESULT*3+1) if operation=='read' else 'x'*(MAX_RESULT*3+3))

    def test_only_new_two_decisions_are_accepted(self):
        for decision in ['blocked','pass','changes_requested']:
            with self.assertRaises(ValueError):parse_verdict(json.dumps({'decision':decision,'summary':'x','findings':[]}))
        with self.assertRaises(ValueError):parse_verdict(json.dumps({'decision':'approve','summary':'x','findings':[{'issue':'bad','required_change':'fix'}]}))
        for payload in [{'decision':'approve','summary':'x'+' '*4001,'findings':[]},
                        {'decision':'request_changes','summary':'x','findings':[{'issue':'x'+' '*2001,'required_change':'fix'}]}]:
            with self.assertRaises(ValueError):parse_verdict(json.dumps(payload))
        self.assertEqual(review_target('request_changes',9,allowance=9),CardStatus.IMPLEMENTATION_QUEUED)
        self.assertEqual(review_target('request_changes',10,allowance=9),CardStatus.REVIEW_CORRECTIONS_PAUSED)

    def test_recoverable_actions_include_continue_and_retry(self):
        card={'status':'review_corrections_paused','repository_scope':'backend','revision':1}
        run={'phase':'review','status':'succeeded','card_revision':1,'result_metadata':{
            'independent_review':{'decision':'request_changes','findings':[{'issue':'x','required_change':'y'}]}}}
        self.assertIn('continue_corrections',_allowed_actions(card,latest_run=run))
        card['status']='failed';run['status']='failed'
        actions=_allowed_actions(card,latest_run=run)
        self.assertIn('retry_review',actions);self.assertIn('cancel',actions)
        self.assertNotIn('continue_corrections',actions)

if __name__=='__main__':unittest.main()
