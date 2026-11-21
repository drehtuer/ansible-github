"""Filters that turn the repository settings into GitHub API requests and
compare them with what GitHub currently reports."""

import json
from copy import deepcopy

from ansible.errors import AnsibleFilterError

PAGES_DEFAULTS = {
    "build_type": "workflow",
    "source_branch": "main",
    "source_path": "/",
    "cname": None,
}
TEMPLATE_KEYS = {"owner", "repository", "include_all_branches"}

# Settings sent with PATCH /repos/{owner}/{repo}; key names match the API.
REPO_FIELDS = [
    "description", "homepage", "visibility", "is_template", "default_branch",
    "has_issues", "has_wiki", "has_projects", "has_discussions",
    "allow_merge_commit", "allow_squash_merge", "allow_rebase_merge",
    "allow_auto_merge", "allow_update_branch", "delete_branch_on_merge",
    "merge_commit_title", "merge_commit_message",
    "squash_merge_commit_title", "squash_merge_commit_message",
]
SECRET_SCANNING = ["secret_scanning", "secret_scanning_push_protection"]


# --- helpers ----------------------------------------------------------------

def _merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        out = deepcopy(base)
        for key, value in override.items():
            out[key] = _merge(base.get(key), value)
        return out
    return deepcopy(override)


def _check_keys(path, value, allowed):
    unknown = set(value) - set(allowed)
    if unknown:
        raise AnsibleFilterError(
            "%s: unknown setting(s) %s; allowed: %s"
            % (path, ", ".join(sorted(unknown)), ", ".join(sorted(allowed))))


def _norm(value):
    """Normalise for comparison: sort lists so order doesn't matter."""
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted((_norm(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


def _covers(current, desired):
    """True if every key in `desired` has the same value in `current`.
    Extra keys GitHub adds to its responses are ignored."""
    if isinstance(desired, dict):
        return isinstance(current, dict) and all(
            _covers(current.get(k), v) for k, v in desired.items())
    return _norm(current) == _norm(desired)


def _show(value):
    return json.dumps(value, sort_keys=True)


# --- settings ---------------------------------------------------------------

def github_settings(repo, name, standard, profiles):
    """Merge standard -> profiles -> repo entry and validate the result."""
    repo = dict(repo or {})
    merged = deepcopy(standard)
    for profile in repo.pop("profiles", []) or []:
        if profile not in profiles:
            raise AnsibleFilterError(
                "%s: unknown profile '%s'; defined: %s"
                % (name, profile, ", ".join(sorted(profiles))))
        _check_keys("profile %s" % profile, profiles[profile] or {}, standard)
        merged = _merge(merged, profiles[profile] or {})
    _check_keys(name, repo, standard)
    merged = _merge(merged, repo)

    for section in ("branch_protection", "tag_protection"):
        _check_keys("%s.%s" % (name, section), merged[section], standard[section])
    if merged["template"] is not None:
        _check_keys(name + ".template", merged["template"], TEMPLATE_KEYS)
    if isinstance(merged["pages"], dict):
        _check_keys(name + ".pages", merged["pages"], PAGES_DEFAULTS)
        merged["pages"] = _merge(PAGES_DEFAULTS, merged["pages"])
        if merged["pages"]["build_type"] not in ("workflow", "legacy"):
            raise AnsibleFilterError(name + ': pages.build_type must be "workflow" or "legacy"')
    elif merged["pages"] not in (None, False):
        raise AnsibleFilterError(name + ": pages must be null, false or a dict")
    if merged["visibility"] not in ("public", "private"):
        raise AnsibleFilterError(name + ': visibility must be "public" or "private"')
    if not (merged["allow_merge_commit"] or merged["allow_squash_merge"] or merged["allow_rebase_merge"]):
        raise AnsibleFilterError(name + ": at least one merge method must be allowed")

    if merged["rulesets_available"] is None:
        merged["rulesets_available"] = merged["visibility"] == "public"
    return merged


def github_create_request(s, name, owner, is_org, api):
    """URL and body to create a repository (optionally from a template)."""
    private = s["visibility"] == "private"
    if s["template"]:
        t = s["template"]
        body = {
            "owner": owner, "name": name, "description": s["description"],
            "private": private,
            "include_all_branches": t.get("include_all_branches", False),
        }
        return {"url": "%s/repos/%s/%s/generate" % (api, t["owner"], t["repository"]), "body": body}
    body = {
        "name": name, "description": s["description"], "homepage": s["homepage"],
        "private": private, "auto_init": s["auto_init"],
    }
    for key in ("gitignore_template", "license_template"):
        if s[key]:
            body[key] = s[key]
    url = "%s/orgs/%s/repos" % (api, owner) if is_org else "%s/user/repos" % api
    return {"url": url, "body": body}


def github_repo_diff(s, current):
    """PATCH body for /repos/{owner}/{repo} with only the differing settings."""
    patch, changes = {}, []
    for key in REPO_FIELDS:
        want, have = s[key], current.get(key)
        if isinstance(want, str) and have is None:
            have = ""
        if want != have:
            patch[key] = want
            changes.append("%s: %s -> %s" % (key, _show(have), _show(want)))

    if s["visibility"] == "public":
        status = "enabled" if s["secret_scanning"] else "disabled"
        sa = current.get("security_and_analysis") or {}
        for key in SECRET_SCANNING:
            have = (sa.get(key) or {}).get("status")
            if have != status:
                patch.setdefault("security_and_analysis", {})[key] = {"status": status}
                changes.append("%s: %s -> %s" % (key, have, status))

    want, have = sorted(s["topics"]), sorted(current.get("topics") or [])
    topics = None
    if want != have:
        topics = {"body": {"names": want}, "change": "%s -> %s" % (_show(have), _show(want))}

    return {"patch": patch, "topics": topics, "changes": changes}


def github_pages_diff(s, current):
    """Pages request: {method, body, changes} or None. `current` is the
    GET /pages response (None if there is no site)."""
    p = s["pages"]
    if p is None:
        return None
    if p is False:
        return {"method": "DELETE", "body": None, "changes": ["pages: remove site"]} if current else None

    body = {"build_type": p["build_type"]}
    if p["build_type"] == "legacy":
        body["source"] = {"branch": p["source_branch"], "path": p["source_path"]}
    if not current:
        return {"method": "POST", "body": body, "changes": ["pages: create %s site" % p["build_type"]]}

    changes = []
    if current.get("build_type") != p["build_type"]:
        changes.append("pages.build_type: %s -> %s" % (current.get("build_type"), p["build_type"]))
    if p["build_type"] == "legacy" and not _covers(current.get("source"), body["source"]):
        changes.append("pages.source: %s -> %s" % (_show(current.get("source")), _show(body["source"])))
    if current.get("cname") != p["cname"]:
        changes.append("pages.cname: %s -> %s" % (current.get("cname"), p["cname"]))
        body["cname"] = p["cname"]
    return {"method": "PUT", "body": body, "changes": changes} if changes else None


# --- rulesets ---------------------------------------------------------------

def _merge_methods(s):
    return [m for m, on in (("merge", s["allow_merge_commit"]),
                            ("squash", s["allow_squash_merge"]),
                            ("rebase", s["allow_rebase_merge"])) if on]


def github_ruleset(s, kind):
    """Desired ruleset body for kind 'default-branch' or 'releases', or None
    if it must not exist."""
    if kind == "default-branch":
        bp = s["branch_protection"]
        if not bp["enabled"]:
            return None
        rules = [{"type": "deletion"}, {"type": "non_fast_forward"}]
        if bp["required_linear_history"]:
            rules.append({"type": "required_linear_history"})
        if bp["require_pull_request"]:
            rules.append({"type": "pull_request", "parameters": {
                "allowed_merge_methods": _merge_methods(s),
                "required_approving_review_count": bp["required_approving_review_count"],
                "dismiss_stale_reviews_on_push": bp["dismiss_stale_reviews_on_push"],
                "required_review_thread_resolution": bp["required_review_thread_resolution"],
                "require_extra_approval_for_unattributed_changes":
                    bp["require_extra_approval_for_unattributed_changes"],
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_reviewers": [],
            }})
        if bp["required_status_checks"]:
            rules.append({"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": c} for c in bp["required_status_checks"]],
                "strict_required_status_checks_policy": False,
                "do_not_enforce_on_create": False,
            }})
        rules.extend(deepcopy(bp["extra_rules"]))
        return {"name": kind, "target": "branch", "enforcement": "active",
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                "bypass_actors": [], "rules": rules}
    if kind == "releases":
        tp = s["tag_protection"]
        if not tp["enabled"]:
            return None
        return {"name": kind, "target": "tag", "enforcement": "active",
                "conditions": {"ref_name": {"include": list(tp["patterns"]), "exclude": []}},
                "bypass_actors": [], "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}]}
    raise AnsibleFilterError("unknown ruleset kind " + kind)


def github_ruleset_find(rulesets, kind, adopt_names):
    """Id of the existing ruleset for `kind` (0 if none): matched by name, else
    by one of the adoptable legacy names (same target)."""
    target = "branch" if kind == "default-branch" else "tag"
    candidates = [r for r in rulesets or [] if r.get("target") == target]
    for names in ([kind], adopt_names.get(kind, [])):
        for r in candidates:
            if r.get("name") in names:
                return r["id"]
    return 0


def github_ruleset_diff(desired, current):
    """List of differences between the desired body and GET /rulesets/{id}."""
    changes = []
    for key in ("name", "target", "enforcement"):
        if current.get(key) != desired[key]:
            changes.append("%s: %s -> %s" % (key, current.get(key), desired[key]))
    if not _covers(current.get("conditions"), desired["conditions"]):
        changes.append("conditions: %s -> %s" % (_show(current.get("conditions")), _show(desired["conditions"])))
    if _norm(current.get("bypass_actors") or []) != _norm(desired["bypass_actors"]):
        changes.append("bypass_actors: %s -> []" % _show(current.get("bypass_actors")))

    have = {r["type"]: r.get("parameters") for r in current.get("rules") or []}
    want = {r["type"]: r.get("parameters") for r in desired["rules"]}
    for rule in sorted(set(have) - set(want)):
        changes.append("rule %s: remove" % rule)
    for rule in sorted(set(want) - set(have)):
        changes.append("rule %s: add" % rule)
    for rule in sorted(set(want) & set(have)):
        if want[rule] and not _covers(have[rule], want[rule]):
            for key, value in want[rule].items():
                current_value = (have[rule] or {}).get(key)
                if not _covers(current_value, value):
                    changes.append("rule %s.%s: %s -> %s" % (rule, key, _show(current_value), _show(value)))
    return changes


# --- profile README ---------------------------------------------------------

README_BEGIN = "<!-- BEGIN repositories: generated by ansible-github, do not edit by hand -->"
README_END = "<!-- END repositories -->"


def github_readme_update(text, section, heading=""):
    """Replace the generated block in `text`, or append it (with `heading`)."""
    block = "%s\n%s\n%s" % (README_BEGIN, section.strip(), README_END)
    start, end = text.find(README_BEGIN), text.find(README_END)
    if start != -1 and end > start:
        return text[:start] + block + text[end + len(README_END):]
    return text.rstrip() + "\n\n" + (heading.strip() + "\n\n" if heading else "") + block + "\n"


class FilterModule(object):
    def filters(self):
        return {
            "github_settings": github_settings,
            "github_create_request": github_create_request,
            "github_repo_diff": github_repo_diff,
            "github_pages_diff": github_pages_diff,
            "github_ruleset": github_ruleset,
            "github_ruleset_find": github_ruleset_find,
            "github_ruleset_diff": github_ruleset_diff,
            "github_readme_update": github_readme_update,
        }
