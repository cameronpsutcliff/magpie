"""Check a file Magpie publishes against its JSON Schema in data/v1/schema, with the standard library only.

`problems(obj, "ideas.schema.json")` returns what is wrong, one line per problem with its place in the file
("$.ideas[3].ingredients[0].url: ..."), and nothing when the file conforms. It reads the published schema file, so the
schema readers download is the one the pipeline enforces.

It implements the JSON Schema 2020-12 keywords Magpie's schemas use: type, const, enum, required, properties,
additionalProperties, items, minItems, maxItems, pattern, minLength, maxLength, format, minimum, maximum, $ref within the
same file, allOf, anyOf, oneOf, not and if/then/else. Formats: date and date-time as RFC 3339 defines them (a leap second
only at 23:59:60 UTC), uri as RFC 3986 (ASCII, valid percent-escapes, a host for http and https) and iri as RFC 3987 (a
uri that may also hold non-ASCII letters, as links copied from sources do). Annotations (title, description, $id and the
like) are skipped. Before anything is checked, the whole schema is read once, every branch and definition included: any
other keyword, format, type or reference raises SchemaError, so a schema can never ask for a check that is silently not
made.

`publishable(obj, schema, label)` is the gate a writer calls before it replaces a published file: it reports the
problems (stderr, and a warning annotation on GitHub Actions) and returns False, and the writer then leaves the last good
file in place.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "data/v1/schema"
ANNOTATIONS = {"$schema", "$id", "$defs", "$comment", "title", "description", "examples", "default", "deprecated"}
KEYWORDS = {"type", "const", "enum", "required", "properties", "additionalProperties", "items", "minItems", "maxItems",
            "pattern", "minLength", "maxLength", "format", "minimum", "maximum", "$ref", "allOf", "anyOf", "oneOf", "not",
            "if", "then", "else"}
FORMATS = {"date", "date-time", "uri", "iri"}
TYPES = {"null", "boolean", "integer", "number", "string", "array", "object"}
# RFC 3986: unreserved, reserved and percent-escaped characters, with at most one fragment. An IRI (RFC 3987) may also
# hold any non-ASCII character that is not a space or a control.
_URI_CHARS = r"[A-Za-z0-9\-._~!$&'()*+,;=:@/?\[\]]|%[0-9A-Fa-f]{2}"
_FRAG_CHARS = r"[A-Za-z0-9\-._~!$&'()*+,;=:@/?]|%[0-9A-Fa-f]{2}"
_WIDE = r"|[^\x00-\x7f\s\x80-\x9f]"
URI = re.compile(rf"[A-Za-z][A-Za-z0-9+.-]*:(?:{_URI_CHARS})*(?:#(?:{_FRAG_CHARS})*)?")
IRI = re.compile(rf"[A-Za-z][A-Za-z0-9+.-]*:(?:{_URI_CHARS}{_WIDE})*(?:#(?:{_FRAG_CHARS}{_WIDE})*)?")
DATE_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:([Zz])|([+-])(\d{2}):(\d{2}))")
MAX_PROBLEMS = 50


class SchemaError(ValueError):
    """The schema uses something this checker does not implement."""


def load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


def _audit(s, root: dict, seen: set[int]) -> None:
    """Read the whole schema once, every branch and definition, and raise SchemaError on anything this checker cannot
    check, whether or not a given file would reach it."""
    if isinstance(s, bool):
        return
    if not isinstance(s, dict):
        raise SchemaError(f"a schema must be an object or a boolean, not {type(s).__name__}")
    if id(s) in seen:
        return
    seen.add(id(s))
    unknown = set(s) - KEYWORDS - ANNOTATIONS
    if unknown:
        raise SchemaError(f"unsupported keywords {sorted(unknown)}")
    types = s.get("type", [])
    for t in types if isinstance(types, list) else [types]:
        if t not in TYPES:
            raise SchemaError(f"unknown type {t!r}")
    if "format" in s and s["format"] not in FORMATS:
        raise SchemaError(f"unknown format {s['format']!r}")
    if "pattern" in s:
        re.compile(s["pattern"])
    if "$ref" in s:
        _resolve(root, s["$ref"])
    subs = [*s.get("properties", {}).values(), *s.get("$defs", {}).values(), *s.get("allOf", []), *s.get("anyOf", []),
            *s.get("oneOf", [])]
    subs += [s[k] for k in ("additionalProperties", "items", "not", "if", "then", "else") if k in s]
    for sub in subs:
        _audit(sub, root, seen)


def _is(v, t: str) -> bool:
    if t == "null":
        return v is None
    if t == "boolean":
        return isinstance(v, bool)
    if t == "integer":
        return not isinstance(v, bool) and (isinstance(v, int) or (isinstance(v, float) and v.is_integer()))
    if t == "number":
        return not isinstance(v, bool) and isinstance(v, (int, float))
    if t == "string":
        return isinstance(v, str)
    if t == "array":
        return isinstance(v, list)
    if t == "object":
        return isinstance(v, dict)
    raise SchemaError(f"unknown type {t!r}")


def _kind(v) -> str:
    for t in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _is(v, t):
            return t
    return type(v).__name__


def _same(a, b) -> bool:
    """JSON equality: 1 equals 1.0, true never equals 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _format_ok(v: str, fmt: str) -> bool:
    if fmt == "date":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            return False
        try:
            dt.date.fromisoformat(v)
        except ValueError:
            return False
        return True
    if fmt == "date-time":
        m = DATE_TIME.fullmatch(v)
        if not m:
            return False
        y, mo, d, h, mi, s = (int(x) for x in m.group(1, 2, 3, 4, 5, 6))
        try:
            dt.date(y, mo, d)
        except ValueError:
            return False
        if h > 23 or mi > 59 or s > 60:
            return False
        off = 0
        if not m.group(7):
            oh, om = int(m.group(9)), int(m.group(10))
            if oh > 23 or om > 59:
                return False
            off = (oh * 60 + om) * (1 if m.group(8) == "+" else -1)
        # A leap second exists only as the last second of a UTC day.
        return s < 60 or (h * 60 + mi - off) % (24 * 60) == 23 * 60 + 59
    if fmt in ("uri", "iri"):
        if not (URI if fmt == "uri" else IRI).fullmatch(v):
            return False
        scheme, rest = v.split(":", 1)
        if scheme.lower() in ("http", "https"):
            host = re.match(r"//(?:[^/?#@]*@)?(\[[^\]]+\]|[^/?#:\[\]]+)(?::(\d*))?(?=[/?#]|$)", rest)
            return bool(host)
        return True
    raise SchemaError(f"unknown format {fmt!r}")


def _resolve(root: dict, ref: str) -> dict | bool:
    if ref == "#":
        return root
    if not ref.startswith("#/"):
        raise SchemaError(f"only references within the same file are supported, not {ref!r}")
    node = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise SchemaError(f"{ref!r} points at nothing")
        node = node[part]
    return node


def _check(v, s, root: dict, path: str, out: list[str]) -> None:
    if s is True:
        return
    if s is False:
        out.append(f"{path}: nothing is allowed here")
        return
    if not isinstance(s, dict):
        raise SchemaError(f"a schema must be an object or a boolean, not {type(s).__name__}")
    unknown = set(s) - KEYWORDS - ANNOTATIONS
    if unknown:
        raise SchemaError(f"unsupported keywords {sorted(unknown)}")
    if "$ref" in s:
        _check(v, _resolve(root, s["$ref"]), root, path, out)
    if "type" in s:
        types = s["type"] if isinstance(s["type"], list) else [s["type"]]
        if not any(_is(v, t) for t in types):
            out.append(f"{path}: is {_kind(v)}, not {' or '.join(types)}")
            return  # the checks below assume the right type
    if "const" in s and not _same(v, s["const"]):
        out.append(f"{path}: must be {json.dumps(s['const'])}")
    if "enum" in s and not any(_same(v, x) for x in s["enum"]):
        out.append(f"{path}: must be one of {json.dumps(s['enum'])}")
    if isinstance(v, str):
        if "minLength" in s and len(v) < s["minLength"]:
            out.append(f"{path}: shorter than {s['minLength']} characters")
        if "maxLength" in s and len(v) > s["maxLength"]:
            out.append(f"{path}: longer than {s['maxLength']} characters")
        if "pattern" in s and not re.search(s["pattern"], v):
            out.append(f"{path}: {json.dumps(v[:80])} does not match {s['pattern']}")
        if "format" in s:
            if s["format"] not in FORMATS:
                raise SchemaError(f"unknown format {s['format']!r}")
            if not _format_ok(v, s["format"]):
                out.append(f"{path}: {json.dumps(v[:80])} is not a valid {s['format']}")
    if _is(v, "number"):
        if "minimum" in s and v < s["minimum"]:
            out.append(f"{path}: below {s['minimum']}")
        if "maximum" in s and v > s["maximum"]:
            out.append(f"{path}: above {s['maximum']}")
    if isinstance(v, dict):
        for k in s.get("required", []):
            if k not in v:
                out.append(f"{path}: {k} is missing")
        props = s.get("properties", {})
        for k, sub in props.items():
            if k in v:
                _check(v[k], sub, root, f"{path}.{k}", out)
        if "additionalProperties" in s:
            for k in v:
                if k not in props:
                    _check(v[k], s["additionalProperties"], root, f"{path}.{k}", out)
    if isinstance(v, list):
        if "minItems" in s and len(v) < s["minItems"]:
            out.append(f"{path}: fewer than {s['minItems']} items")
        if "maxItems" in s and len(v) > s["maxItems"]:
            out.append(f"{path}: more than {s['maxItems']} items")
        if "items" in s:
            for i, x in enumerate(v):
                _check(x, s["items"], root, f"{path}[{i}]", out)
                if len(out) >= MAX_PROBLEMS:
                    return
    for sub in s.get("allOf", []):
        _check(v, sub, root, path, out)
    if "anyOf" in s and not any(not _sub(v, sub, root, path) for sub in s["anyOf"]):
        out.append(f"{path}: matches none of the allowed shapes")
    if "oneOf" in s:
        n = sum(1 for sub in s["oneOf"] if not _sub(v, sub, root, path))
        if n != 1:
            out.append(f"{path}: matches {n} of the allowed shapes, not exactly one")
    if "not" in s and not _sub(v, s["not"], root, path):
        out.append(f"{path}: has a shape that is not allowed")
    if "if" in s:
        branch = s.get("then") if not _sub(v, s["if"], root, path) else s.get("else")
        if branch is not None:
            _check(v, branch, root, path, out)


def _sub(v, s, root: dict, path: str) -> list[str]:
    found: list[str] = []
    _check(v, s, root, path, found)
    return found


def problems(obj, schema: dict | str) -> list[str]:
    """What is wrong with obj under the schema (a dict, or a file name in data/v1/schema), at most MAX_PROBLEMS lines."""
    root = load(schema) if isinstance(schema, str) else schema
    _audit(root, root, set())
    out: list[str] = []
    _check(obj, root, root, "$", out)
    return out[:MAX_PROBLEMS]


def publishable(obj, schema: str, label: str) -> bool:
    """True when obj conforms. Otherwise the problems are reported and the caller keeps the last good file."""
    try:
        bad = problems(obj, schema)
    except (OSError, ValueError) as ex:  # an unreadable or unsupported schema is a failed check, not a pass
        bad = [f"the schema could not be used: {type(ex).__name__}: {str(ex)[:160]}"]
    if not bad:
        return True
    line = (f"{label} not published: it fails {schema} ({len(bad)} problem{'s' if len(bad) != 1 else ''}"
            f"{', first ' + str(MAX_PROBLEMS) if len(bad) >= MAX_PROBLEMS else ''}); the last good file stays live. "
            + "; ".join(bad[:5]))
    print(line, file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=Magpie schema check::{line}")
    return False
