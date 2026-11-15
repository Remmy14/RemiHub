# Deployment file eligibility

After the Implementer produces an artifact snapshot, backend/web candidates run
`GitBackendDeploymentManager.deployment_eligibility` before trusted validation.
It invokes the deployment path and Git mode guards and shared migration file
planner. Deployment independently rechecks the approved artifact, source identity,
file policy, migrations, QA provenance, authorization and target refs as before.
The evaluator grants no approval or authority. Android retains its existing flow.

`frontend-web/tests/` accepts nested, non-executable UTF-8 `*.test.mjs`,
`*.spec.mjs`, `*.test.js`, and `*.spec.js` files with bounded alphanumeric,
underscore, hyphen and dot names. Existing src/public extensions remain intact.
Environment, secret-like, generated dependency/dist paths, traversal, symlinks,
special files and unsafe Git modes remain disallowed. Both trusted frontend
validators execute matching tests after offline npm installation with Node's
strip-types support; no package-supplied test command is trusted.

The evaluator returns exact paths, rule identifiers, reasons, effective policy
constants and the policy source hash. Successful evidence participates in the
candidate digest and is supplied to the reviewer alongside trusted validation.
The reviewer retains only Approve and Request Changes and unrestricted authorized
source inspection; eligibility is an execution check, not a model verdict.

Correctable file violations complete the implementation run with its original
snapshot and digest, then transactionally queue another implementation run under
the owned lease. Feedback describes location/format/inclusion corrections without
instructing blanket deletion. A separate three-correction budget preserves review
ordinals; failure and retry retain its context. The corrected candidate is checked
again before trusted validation and review. Protected infrastructure changes and
legitimate frontend tests rejected solely by path policy fail operationally for
operator remediation, retaining the artifact and diagnostic in history. Budget
exhaustion is also a recoverable execution failure, not a permanent card state.

Installation must retain the original dashboard base until that approved
candidate completes normal QA and production deployment. Stage and independently
review an immutable infrastructure runtime and validator first; pause unrelated
workers during recovery to avoid source-parity races. Activate only reviewed
worker code and validators, leaving the six canonical refs at the original base.
Use authenticated Retry Failed Run for the original card with its existing
approval binding. After genuine production success, integrate the exact reviewed
infrastructure delta on the dashboard release, independently review that exact
combined source, and use the existing retained-ref, compare-and-swap and clean
fast-forward promotion primitives for all six refs. Never change the approved
dashboard artifact or fabricate deployment/QA records. Keep private rollback
copies of helpers and service overrides; restore only known changed settings.
