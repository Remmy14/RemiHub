# Permanent auth-aware release verification hardening

Integrate `PERMANENT-AUTH-AWARE-RELEASE-VERIFICATION-PATCH.diff` into the
canonical tracked Android release-helper source.

Permanent tests must prove:

- OpenAPI returns 200 and advertises both app-update routes.
- Unauthenticated latest and download requests return 401 with a Bearer
  challenge.
- Unauthenticated 200 responses are rejected.
- A 401 without a Bearer challenge is rejected.
- Active release and APK identity are verified independently of HTTP auth.
- The protected helper never handles Firebase passwords or tokens.
- Successful release journals retain HTTP-boundary evidence.
