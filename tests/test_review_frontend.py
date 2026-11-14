"""Execute actual TypeScript presentation and action clients without browser/network."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

@unittest.skipUnless(shutil.which('node'), 'Node is required for real TypeScript contract tests')
class ReviewFrontendTests(unittest.TestCase):
    def node(self,script):
        p=subprocess.run(['node','--experimental-strip-types','--input-type=module','-e',script],
            cwd=ROOT,text=True,capture_output=True,timeout=30)
        self.assertEqual(p.returncode,0,p.stderr);return json.loads(p.stdout)

    def test_decision_badges_never_show_negative_review_as_succeeded(self):
        script='''import {reviewRunStatus} from './frontend-web/src/api/agentReview.ts';
const fixture=(status,decision,attempt=1)=>({phase:'review',status,independent_review:decision ? {decision,review_attempt:attempt,correction_allowance:3}:null});
console.log(JSON.stringify([
 reviewRunStatus(fixture('succeeded','approve')),
 reviewRunStatus(fixture('succeeded','request_changes')),
 reviewRunStatus(fixture('succeeded','request_changes',4)),
 reviewRunStatus(fixture('failed',null)),
 reviewRunStatus(fixture('succeeded',null))]));'''
        self.assertEqual(self.node(script),['review_approved','changes_requested','automatic_corrections_paused','review_execution_failed','review_execution_finished'])

    def test_actual_client_binds_continue_and_retry_to_exact_candidate(self):
        source=(ROOT/'frontend-web/src/api/agentApi.ts').read_text()
        stub='''const sent: unknown[]=[];
async function apiRequest<T>(path:string, options:unknown):Promise<T> {
 sent.push({path,options});return {success:true,data:{}} as T;
}'''
        source=source.replace('import { apiRequest } from "./authenticatedApi";',stub)
        with tempfile.TemporaryDirectory(prefix='review-ts-client-') as directory:
            path=Path(directory)/'client.ts';path.write_text(source+'\nexport {sent};\n')
            script=f'''import {{AgentActions,performAgentAction,reviewRevisionForCard,sent}} from {json.dumps(path.as_uri())};
const digest='a'.repeat(64);
const run={{id:'review-id',phase:'review',status:'succeeded',card_revision:1,result_metadata:{{}},independent_review:{{candidate_digest:digest,decision:'request_changes',findings:[{{issue:'context',required_change:'inspect'}},{{issue:'defect',required_change:'fix'}}]}}}};
const card={{id:'card-id',revision:1,status:'review_corrections_paused',allowed_actions:[AgentActions.CONTINUE_CORRECTIONS],latest_run:run,runs:[run]}};
const revision=reviewRevisionForCard(card,{{0:false}},'The first finding was a source-context omission; correct the real defect.');
await performAgentAction(card,AgentActions.CONTINUE_CORRECTIONS,'Correct real defect',revision);
run.status='failed';run.independent_review=null;run.result_metadata={{review_source:{{implementation_metadata:{{candidate_digest:digest}}}}}};
card.status='failed';card.allowed_actions=[AgentActions.RETRY_REVIEW];
await performAgentAction(card,AgentActions.RETRY_REVIEW);
console.log(JSON.stringify(sent));'''
            calls=self.node(script)
        self.assertEqual(calls[0]['path'],'/agent/cards/card-id/reviews/review-id/continue-corrections')
        body=json.loads(calls[0]['options']['body']);self.assertEqual(body['finding_indexes'],[1]);self.assertEqual(body['candidate_digest'],'a'*64)
        self.assertNotIn('actor_user_id',body)
        self.assertEqual(calls[1]['path'],'/agent/cards/card-id/reviews/review-id/retry')
        self.assertEqual(json.loads(calls[1]['options']['body'])['candidate_digest'],'a'*64)

if __name__=='__main__':unittest.main()
