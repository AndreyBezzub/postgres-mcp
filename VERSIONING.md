# Versioning

This repository is a fork of
[crystaldba/postgres-mcp](https://github.com/crystaldba/postgres-mcp). It carries
downstream changes (e.g. multi-database support) on top of an upstream baseline.

## Fork version scheme

Fork releases are tagged:

```
vMAJOR.MINOR.PATCH-hc.N
```

- `MAJOR.MINOR.PATCH` follows [SemVer](https://semver.org/) and is derived from the nature
  of the changes since the upstream baseline, classified via
  [Conventional Commits](https://www.conventionalcommits.org/):
  - `feat:` → **MINOR** bump
  - `fix:` → **PATCH** bump
  - a commit marked `!` / `BREAKING CHANGE:` → **MAJOR** bump
- The `-hc.N` pre-release suffix (a SemVer pre-release identifier) distinguishes fork
  releases from upstream's own tags, so the two version lines never collide and a future
  rebase onto a new upstream release stays unambiguous. `N` increments for each fork
  release that shares the same `MAJOR.MINOR.PATCH`.

**Example:** upstream baseline `0.3.0` + a backward-compatible multi-database feature
(`feat:`) → `v0.4.0-hc.1`.

## Branch / merge policy

- Feature work happens on topic branches (e.g. `feat/...`) with granular Conventional Commits.
- A completed feature is **squash-merged** into `main` as a single Conventional Commit, keeping
  `main` a clean, rebasable delta over upstream. The topic branch is retained so the granular
  history remains available for `git bisect` / `git blame`.
- Release tags are cut on `main`.
