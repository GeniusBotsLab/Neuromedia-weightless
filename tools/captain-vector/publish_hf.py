#!/usr/bin/env python3
"""publish_hf.py — push a GLP control-vector GGUF + model card to HuggingFace.

Does, in order:
  1. create_repo (public, exist_ok) — repo_type="model"
  2. update_repo_settings(gated="auto") — the Responsible-Use-Agreement
     checkbox lives in the card's extra_gated_* frontmatter
  3. upload README.md (the card) then the .gguf
  4. re-download the .gguf and assert sha256 byte-identity with the local file

Stdlib + huggingface_hub only. Run with a python that has hub >= 1.24, e.g.
/opt/homebrew/Cellar/hf/*/libexec/bin/python.

Usage:
  python publish_hf.py FILE.gguf --repo msuiche/<name> --card README.md
"""
import argparse
import hashlib
import os
import sys

from huggingface_hub import HfApi, hf_hub_download


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gguf", help="local .gguf path")
    ap.add_argument("--repo", required=True, help="e.g. msuiche/gemma-3-1b-it-...")
    ap.add_argument("--card", required=True, help="model card markdown -> README.md")
    a = ap.parse_args()

    assert os.path.isfile(a.gguf), a.gguf
    assert os.path.isfile(a.card), a.card
    fname = os.path.basename(a.gguf)
    api = HfApi()
    who = api.whoami()["name"]
    print(f"[auth] {who}")

    api.create_repo(a.repo, repo_type="model", private=False, exist_ok=True)
    api.update_repo_settings(a.repo, gated="auto")
    print(f"[repo] https://huggingface.co/{a.repo} (public, gated=auto)")

    api.upload_file(path_or_fileobj=a.card, path_in_repo="README.md",
                    repo_id=a.repo, repo_type="model")
    print("[upload] README.md")
    api.upload_file(path_or_fileobj=a.gguf, path_in_repo=fname,
                    repo_id=a.repo, repo_type="model")
    print(f"[upload] {fname} ({os.path.getsize(a.gguf)} bytes)")

    local_sha = sha256(a.gguf)
    got = hf_hub_download(a.repo, fname, repo_type="model")
    remote_sha = sha256(got)
    ok = local_sha == remote_sha
    print(f"[verify] local  sha256 {local_sha}")
    print(f"[verify] remote sha256 {remote_sha} (re-downloaded)")
    print(f"[verify] {'PASS — byte-identical' if ok else 'FAIL — MISMATCH'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
