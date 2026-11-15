import tempfile
import unittest
from pathlib import Path
from backend.core.agent_deployment import GitBackendDeploymentManager as Policy

class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def evaluate(self, name, content='safe text\n', mode=0o644):
        p=self.root/name
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(content)
        p.chmod(mode)
        return Policy.deployment_eligibility((name,),self.root,b'')

    def test_supported_files(self):
        for name in ('frontend-web/tests/usageMetrics.test.mjs','frontend-web/tests/nested/quota.spec.js',
                     'frontend-web/src/a.tsx','frontend-web/src/a.ts','backend/example.py',
                     'tests/test_example.py','docs/example.md','requirements.txt'):
            with self.subTest(name=name): self.assertTrue(self.evaluate(name)['eligible'])

    def test_rejected_files(self):
        for name in ('frontend-web/tests/script.mjs','frontend-web/src/script.mjs',
                     'frontend-web/tests/a.sh','frontend-web/node_modules/a.test.mjs',
                     'frontend-web/tests/node_modules/a.test.mjs','frontend-web/dist/a.test.mjs',
                     'frontend-web/tests/.env','frontend-web/tests/private-key.test.mjs',
                     'frontend-web/tests/credentials.test.mjs','other/script.mjs'):
            with self.subTest(name=name):
                result=self.evaluate(name)
                self.assertFalse(result['eligible'])
                self.assertEqual(result['issues'][0]['path'],name)

    def test_unsafe_paths_and_modes(self):
        for name in ('../escape.py','/tmp/escape.py','frontend-web//src/a.ts','frontend-web/src/../a.ts'):
            self.assertFalse(Policy.deployment_eligibility((name,),self.root,b'')['eligible'])
        self.assertFalse(self.evaluate('frontend-web/tests/a.test.mjs',mode=0o755)['eligible'])
        self.assertFalse(self.evaluate('frontend-web/tests/a.test.mjs',content='a\0b')['eligible'])
        self.assertFalse(Policy.deployment_eligibility(('frontend-web/tests/link.test.mjs',),self.root,b'diff --git a/frontend-web/tests/link.test.mjs b/frontend-web/tests/link.test.mjs\nnew file mode 120000')['eligible'])

    def test_symlink(self):
        target=self.root/'target';target.write_text('safe')
        (self.root/'frontend-web').mkdir();(self.root/'frontend-web/tests').symlink_to(self.root,target_is_directory=True)
        self.assertFalse(Policy.deployment_eligibility(('frontend-web/tests/target.test.mjs',),self.root,b'')['eligible'])

    def test_all_rejections_reported(self):
        r=Policy.deployment_eligibility(('other/a.sh','other/b.exe'),self.root,b'')
        self.assertEqual(len(r['issues']),2)
        self.assertFalse(r['deployment_authority'])

    def test_legitimate_policy_mismatch(self):
        r=self.evaluate('frontend-web/unsupported/quota.test.mjs')
        self.assertEqual(r['issues'][0]['remediation'],'operator_required')

    def test_corrected_candidate(self):
        self.assertFalse(self.evaluate('other/a.sh')['eligible'])
        self.assertTrue(self.evaluate('frontend-web/tests/a.test.mjs')['eligible'])

    def test_shared_migration_file_policy(self):
        import subprocess
        def git(*args):
            return subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)
        git('init','-q');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-qm','base')
        prefix='backend/database/migrations/'
        up=prefix+'0023_example.up.sql';down=prefix+'0023_example.down.sql'
        p=self.root/up;p.parent.mkdir(parents=True);p.write_text('CREATE TABLE eligibility_fixture(id integer);')
        result=Policy.deployment_eligibility((up,),self.root,b'')
        self.assertFalse(result['eligible']);self.assertIn('both up and down',result['issues'][0]['reason'])
        (self.root/down).write_text('DROP TABLE eligibility_fixture;')
        self.assertTrue(Policy.deployment_eligibility((up,down),self.root,b'')['eligible'])
        git('add','.');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','migration')
        result=Policy.deployment_eligibility((up,),self.root,('--- a/'+up+'\n').encode())
        self.assertFalse(result['eligible']);self.assertIn('Historical migration',result['issues'][0]['reason'])
