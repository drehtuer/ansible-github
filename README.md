# ansible-github

Manages the configuration of all my GitHub repositories with Ansible, so every
repo has the same settings and new repos can be created with one entry.
There is no state: every run compares GitHub with the config and fixes the
differences.

## Layout

| File | Purpose |
| --- | --- |
| `group_vars/all/standard.yml` | **The standard configuration.** Every default and every available setting lives here. |
| `group_vars/all/repos.yml` | **The list of public repositories** and the **profiles**. One entry per repo, with only the settings that differ from the standard. |
| `group_vars/all/vault.yml` | The private repositories. **Encrypted** with `ansible-vault`, so their names stay private. |
| `roles/github_repo/tasks/` | What a "standard repo" is made of: settings, topics, Dependabot, immutable releases, Pages, rulesets. |
| `roles/github_profile_readme/` | Writes the repository table (badges for latest release, open issues, PRs, build status) into the profile README (`<owner>/<owner>`). |
| `filter_plugins/github.py` | Merging and validating settings, comparing them with GitHub. |
| `github.yml` | The playbook. |

## Usage

Needs Ansible (core 2.16+) and a GitHub token with the `repo` scope, taken
from `GITHUB_TOKEN` or else from `gh auth token`.

```sh
ansible-playbook github.yml --check    # show differences, change nothing
ansible-playbook github.yml            # apply
ansible-playbook github.yml -e github_only=ShotgunApp,dotfiles
ansible-playbook github.yml --tags repos    # only repository settings
ansible-playbook github.yml --tags readme   # only the profile README
```

`--check` is the drift report: every repo whose configuration differs from
the standard is listed with each difference (`setting: current -> desired`).
The vault password is read from `vault/secret` (gitignored; keep a copy in
your password manager — without it, `vault.yml` can't be decrypted).

## Creating a new repository

Add an entry to `github_repos` in `group_vars/all/repos.yml` (or, for a private
repo, to `github_private_repos` via `ansible-vault edit group_vars/all/vault.yml`)
and run the playbook:

```yaml
my-new-repo:
  description: What it does
  topics: [android, ai-coding]
```

That's it — it gets the standard settings. Creation-only options:
`gitignore_template`, `license_template`, `auto_init` (default `true`) and
`template: {owner: ..., repository: ...}`.

## The standard

| Area | Default |
| --- | --- |
| Visibility | public |
| Features | issues and pull requests on; wiki, projects, discussions, sponsorships off |
| Merging | merge commit + rebase allowed, squash off; auto-merge on; always suggest updating PR branches; delete head branch on merge |
| Commit messages | PR title + PR body |
| Security | Dependabot alerts + security updates; secret scanning + push protection (public) |
| Releases | immutable releases on |
| GitHub Pages | built by GitHub Actions (public repos only) |
| Default branch ruleset `main` | PR required (0 approvals, stale approvals dismissed on new commits, conversations must be resolved, extra approval for unattributed changes); force pushes blocked; deletion blocked; merge methods follow the repo's merge settings |
| Tag ruleset `releases` | tags matching `v*` can't be deleted or force-pushed |

## Profiles and overrides

Settings are merged in this order: **standard → profiles (in listed order) →
the repo's own entry**. Dicts merge key by key; lists and plain values replace.
Unknown keys are an error, so typos fail the run instead of being ignored.

A profile is a named set of settings in `github_profiles` that applies to only
some repos:

```yaml
github_profiles:
  homepage:              # homepage points to the repo's Pages site
    homepage: "https://{{ github_owner }}.github.io/{{ repo_name }}/"
  codeql:                # merges must pass CodeQL + code quality
    branch_protection:
      extra_rules: [...]

github_repos:
  ShotgunApp:
    profiles: [homepage, codeql]
    branch_protection:
      required_status_checks: [Build, Unit tests, Static analysis]
```

Any setting from the standard can be overridden in a profile or a repo, e.g.
`has_wiki: true`, `allow_squash_merge: true`, `tag_protection: {enabled: false}`.
`branch_protection.extra_rules` takes raw [ruleset rules](https://docs.github.com/en/rest/repos/rules)
as the GitHub API returns them, so any rule type can be used.

GitHub Pages: every public repo gets an Actions-based site (`pages: {}`). A site
stays empty until the repo has a workflow that deploys it
(`actions/upload-pages-artifact` + `actions/deploy-pages`). Per repo:
`pages: {build_type: legacy, source_branch: main, source_path: /}` for a
branch-based site, `pages: false` to remove it, `pages: null` to leave it
unmanaged. Private repos are never given a site.

## Profile README

`roles/github_profile_readme` lists every public repo from `repos.yml` in the
README of the profile repo, with [shields.io](https://shields.io) badges for
the latest release, open issues, open pull requests and the build status (check runs on the default
branch; `–` for repos without workflows). The badges update live; the playbook
only needs to run when repos are added, removed or renamed.

Only the block between the `BEGIN repositories` / `END repositories` comments
is rewritten; the rest of the README is free text. Set `profile_readme: false`
on a repo to leave it out. Private repos are never listed. The profile repo's
`branch_protection` must stay disabled, because the table is committed directly
to `main`.

## Caveats

- **Removing a repo from the list does not archive or delete it**; it is just
  no longer managed. To archive it, set `archived: true` and run once.
- **Repos not in the list are never touched**, including the archived ones.
- **Private repos get no rulesets / secret scanning** — GitHub Free doesn't
  offer them on private repos. With GitHub Pro, set `rulesets_available: true`
  (in the standard or per repo).
- **Rulesets are matched by name** (`branch_protection.name`,
  `tag_protection.name`). Rulesets named `default-branch` or `Releases` are
  adopted and renamed (`github_ruleset_adopt_names`). Rulesets with other names
  are left alone. `enabled: false` deletes the managed ruleset.
- **"Auto-close issues with merged linked pull requests"** has no API (neither
  REST nor GraphQL). Set it by hand in each repo's settings.
- **Encrypt `vault.yml` before committing it.** The playbook refuses to run
  while it is unencrypted.
