"""The redaction gate: anything Magpie writes for the public passes through scrub() first."""
from __future__ import annotations

import json
import re

from .util import ROOT

FORBIDDEN = [re.compile(p, re.I) for p in (r"/Users/", r"/home/[a-z]", r"sk-ant-[A-Za-z0-9]", r"ghp_[A-Za-z0-9]{20}",
                                            r"github_pat_[A-Za-z0-9_]{20}", r"xai-[A-Za-z0-9]{20}")]
# Machine names, internal hosts and folder names never ship in the repo either: list them, one regex
# per line, in the untracked file .redact.local on the machine that runs the home tier.
_local = ROOT / ".redact.local"
if _local.exists():
    FORBIDDEN += [re.compile(l.strip(), re.I) for l in _local.read_text().splitlines() if l.strip() and not l.startswith("#")]


def scrub(obj):
    text = json.dumps(obj, ensure_ascii=False)
    hits = [p.pattern for p in FORBIDDEN if p.search(text)]
    if hits:
        raise SystemExit(f"redaction gate blocked publish: {hits}")
