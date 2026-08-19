# CHANGELOG

<!-- version list -->

## v1.2.2 (2026-08-19)

### Bug Fixes

- **workspace**: Trust the triggering session over raw mtime
  ([`3328204`](https://github.com/GianCdM/memex/commit/3328204aaeed4aa0f95be8e3706335c98a566c97))


## v1.2.1 (2026-08-19)

### Chores

- **ci**: Pass --no-vcs-release to semantic-release
  ([`811bd7e`](https://github.com/GianCdM/memex/commit/811bd7ef01616b5a8c93215da04cbf21c3564d5d))


## v1.2.0 (2026-08-19)

### Bug Fixes

- **capture**: Advance ignored transcript windows
  ([`ca1761d`](https://github.com/GianCdM/memex/commit/ca1761da6286f5b371c05a4b81e2c6a4a9836a92))

- **synth**: Delay chunked capture cursor
  ([`a8a15ea`](https://github.com/GianCdM/memex/commit/a8a15eabae083e72a1ef6f1770b52b4c6bd54ef0))

- **synth**: Unblock cold-start capture-delta chains
  ([`991f757`](https://github.com/GianCdM/memex/commit/991f7572a7b3415ed7d2d906ae5180c1b706dd41))

### Features

- **capture**: Add byte-offset transcript cursors
  ([`7f858ad`](https://github.com/GianCdM/memex/commit/7f858ad7d88702ac0fba497d9dddec4ed760c40e))

### Refactoring

- **capture**: Remove unused import
  ([`e1612d6`](https://github.com/GianCdM/memex/commit/e1612d68d30ce561d53c3d2978c3eeef913e317b))


## v1.1.0 (2026-08-18)

### Documentation

- Add AGENTS.md operational briefing for agents
  ([`7fc11fb`](https://github.com/GianCdM/memex/commit/7fc11fb2c43980fd49803afcfb3ab13cf2880044))

- Simplify Memory layers table, add version badge + note, fix test count
  ([`0dc081f`](https://github.com/GianCdM/memex/commit/0dc081f71f9c5207016c6d0c16bf8b5ffe0540fa))

### Features

- Harden merge/verify prompts against invented content
  ([`e020a6d`](https://github.com/GianCdM/memex/commit/e020a6d3aece1d905ae50e9119ed14bb761fee81))


## v1.0.2 (2026-08-18)

### Chores

- Disable GitHub release, document versioning in README
  ([`df5fe6b`](https://github.com/GianCdM/memex/commit/df5fe6b9f880cebc4628d51de079e8ca05507167))

### Performance Improvements

- Collapse consecutive same-role turns in Claude parser
  ([`187453e`](https://github.com/GianCdM/memex/commit/187453e046742497f29b8db2b4b6d3cfb98331aa))


## v1.0.1 (2026-08-18)

### Bug Fixes

- Resolve version from pyproject.toml when running from source
  ([`f8800d9`](https://github.com/GianCdM/memex/commit/f8800d949aa8654f8991385d196a294ad0cd5704))

### Documentation

- Add conventional commit conventions for semantic-release
  ([`ca74833`](https://github.com/GianCdM/memex/commit/ca748336470ae78c9e465c0531711bd0669689d4))


## v1.0.0 (2026-08-18)

- Initial Release
