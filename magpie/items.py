"""The normalized item every collector emits."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .util import canonical_keys, short_hash, url_key


@dataclass
class Item:
    source_id: str          # registry id, e.g. "github-trending-daily"
    source_kind: str        # repo | model | space | paper | news | newsletter | blog | changelog | video | social | community
    source_name: str        # display name of the source
    url: str
    title: str
    summary: str = ""
    published_at: str | None = None
    author: str | None = None
    vendor_hint: str | None = None
    trust: float = 0.6      # 0..1, how much a mention from this source counts
    metrics: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)
    keys: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)  # keys referenced in the body, not the subject

    def finalize(self, body: str = "") -> "Item":
        if not self.keys:
            own = canonical_keys(self.url)
            self.keys = own or [url_key(self.url)]
        if body:
            self.mentions = [k for k in canonical_keys(None, body) if k not in self.keys][:40]
        return self

    @property
    def id(self) -> str:
        return short_hash(f"{self.source_id}|{self.url}|{self.title}")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["id"] = self.id
        return d
