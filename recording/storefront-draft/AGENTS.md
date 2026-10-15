# AGENTS.md

- Before every push to main, run the full test suite: `make test` (about 4 minutes).
- Why: main deploys to production on push, with no CI gate in between. Changes that pass their own module's tests
  still sometimes break another part of the site, and the full suite is how those are caught.
- `make test-unit` runs the module tests only (seconds). Use it for quick checks while you work; it does not replace
  `make test` before a push.
