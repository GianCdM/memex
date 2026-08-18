@.claude/prism.md

# Commit conventions (semantic-release)

Every push to `main` triggers the release workflow (`.github/workflows/release.yml`),
which reads the **conventional commits** since the last tag and bumps the version.
The version's single source of truth is `pyproject.toml [project] version`; the tag
is the release point. There is no GitHub Release page (tag + changelog only).

Write every commit message with the conventional prefix so the bump is deliberate:

| Prefix                 | Bump | Example                                    |
|------------------------|------|--------------------------------------------|
| `feat:`                | MINOR | `feat: add semantic recall to search`    |
| `fix:`                 | PATCH | `fix: migrate old provider shape first`  |
| `feat`/`fix` + `BREAKING CHANGE:` | MAJOR | `feat: rework API (BREAKING CHANGE: ...)` |
| `chore:` `docs:` `refactor:` `test:` | none | `refactor: simplify complete()`          |

Rules to follow:
- **Scope optional** but encouraged on touchy areas: `fix(cli):`, `feat(config):`.
- **`refactor:`, `chore:`, `docs:`, `test:` do NOT bump** — safe for non-releasing work.
- A breaking change MUST be signaled with `BREAKING CHANGE:` in the body/footer (not
  just a `!`), so the parser catches it and bumps MAJOR.
- The workflow commits the bump with `[skip ci]` and tags `vX.Y.Z`; it will not
  re-trigger itself. There is no manual version edit — let the workflow own the bump.
- Don't create or move tags manually for releases (except the initial baseline),
  and never edit `pyproject.toml` version by hand.

One push = at most one release. If unsure whether a change is `feat`, `fix`, or
none, prefer `refactor:`/`chore:` (no bump) unless the user wants a release.