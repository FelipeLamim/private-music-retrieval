#!/usr/bin/env python3
"""Fetch public metadata for the HE-PIR engineering survey (read-only GitHub REST API + README).

Writes results/sealpir/survey/github_meta.json and survey/readmes/<owner>__<repo>.md.
Only public endpoints, no token, no uploads. Run from the project root after `source ./env.sh`.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPOS = [
    "microsoft/SealPIR",
    "menonsamir/spiral",
    "menonsamir/spiral-rs",
    "blyssprivacy/sdk",
    "ahenzinger/simplepir",
    "brave-experiments/frodo-pir",
    "menonsamir/ypir",
    "google/hintless_pir",
    "mhmughees/Onion-PIR",
    "ishtiyaque/FastPIR",
    "XPIR-team/XPIR",
    "google/private-retrieval",
    "apple/swift-homomorphic-encryption",
]
OUT = os.path.join(os.environ.get("PMR_ROOT", "."), "results", "sealpir", "survey")
API = "https://api.github.com/repos/"


def get(url, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "tcc-sealpir-survey"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main():
    os.makedirs(os.path.join(OUT, "readmes"), exist_ok=True)
    meta = {"fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "repos": {}}
    for full in REPOS:
        entry = {"url": "https://github.com/" + full}
        st, body = get(API + full)
        entry["repo_status"] = st
        if st == 200:
            d = json.loads(body)
            for k in ("full_name", "description", "language", "default_branch", "created_at",
                      "pushed_at", "archived", "fork", "stargazers_count", "open_issues_count", "html_url"):
                entry[k] = d.get(k)
            entry["license"] = (d.get("license") or {}).get("spdx_id")
            br = d.get("default_branch")
            st2, body2 = get(API + full + "/commits?per_page=1&sha=" + br)
            if st2 == 200 and json.loads(body2):
                c = json.loads(body2)[0]
                entry["last_commit_sha"] = c["sha"]
                entry["last_commit_date"] = c["commit"]["committer"]["date"]
            st3, body3 = get(API + full + "/releases?per_page=1")
            rel = json.loads(body3) if st3 == 200 else []
            entry["latest_release"] = ({"tag": rel[0]["tag_name"], "published_at": rel[0]["published_at"]}
                                       if rel else None)
            # README from raw.githubusercontent.com (does not count against the API rate limit).
            for name in ("README.md", "readme.md", "README.rst", "README"):
                st5, body5 = get("https://raw.githubusercontent.com/%s/%s/%s" % (full, br, name), accept="*/*")
                if st5 == 200:
                    path = os.path.join(OUT, "readmes", full.replace("/", "__") + "__" + name)
                    with open(path, "wb") as f:
                        f.write(body5)
                    entry["readme_saved"] = os.path.relpath(path, OUT)
                    break
        meta["repos"][full] = entry
        print(full, entry.get("repo_status"), entry.get("license"), entry.get("last_commit_date"),
              entry.get("latest_release"), flush=True)
    with open(os.path.join(OUT, "github_meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
