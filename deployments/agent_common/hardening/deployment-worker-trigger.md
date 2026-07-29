# Deployment worker triggering and dynamic Git trust

Deployment approval and deployment retry commit the queued run first, then request the exact repository-scoped run-once deployment service through a root-owned argument-validating helper. The web application receives sudo permission only for the two exact helper invocations.

The backend and Android deployment timers are fallback safety nets. They start the matching run-once worker once per minute when inactive, allowing a queued or newly eligible blocked deployment to recover from a missed immediate trigger or a service restart.

Deployment Git commands add one command-local `safe.directory` entry for the exact resolved repository/worktree path. The fixed protected global Git config remains enabled, and wildcard Git trust is forbidden.
