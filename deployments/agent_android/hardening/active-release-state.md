# Permanent active-release state hardening

Integrate `PERMANENT-ACTIVE-RELEASE-STATE-PATCH.diff` into the canonical
protected Android release-helper source.

Permanent tests must prove:

1. The live state query returns `platform` for every Android release row.
2. The active row contains exactly the fields consumed by publication
   verification.
3. An active Android row with the expected release identity passes.
4. A missing or non-Android platform fails.
5. Publication success cannot be reported unless database activation, APK
   identity, source promotion, and authentication-boundary checks all pass.
6. A verification failure after activation still produces a complete,
   error-free rollback.
