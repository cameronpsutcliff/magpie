r"""Check a file Magpie publishes against its JSON Schema in data/v1/schema, with the standard library only.

`problems(obj, "ideas.schema.json")` returns what is wrong, one line per problem with its place in the file
("$.ideas[3].ingredients[0].url: ..."), and nothing when the file conforms. It reads the published schema file, so the
schema readers download is the one the pipeline enforces.

It checks a stated subset of JSON Schema 2020-12, the part Magpie's schemas use, and refuses any schema outside it with
SchemaError rather than read it loosely. The subset: one schema resource ($id only at the root, $schema absent or the
2020-12 URI); the keywords type, const, enum, required, properties, additionalProperties, items, minItems, maxItems,
pattern, minLength, maxLength, format, minimum, maximum, $ref, allOf, anyOf, oneOf, not and if/then/else; $ref only as a
URI fragment holding a JSON Pointer into the same document (RFC 6901); the annotations title, description, $comment,
examples, default, deprecated and $defs, each of its own type. format is asserted, as the format-assertion vocabulary
asks, not only annotated.

A pattern is an ECMA-262 regex read in Unicode mode (the u flag), as JSON Schema 2020-12 asks (core, section 6.4), so it
matches code points, as Python does. The checker takes only the part of that syntax Python reads the same way (literals;
\d \D \w \W, read as ASCII; character classes; ^, and $ as the end of the string; groups; alternation; quantifiers, lazy
or not; escapes of syntax characters and /) and refuses the rest (., \s, \b, lookarounds, backreferences, named groups,
property escapes, possessive or stacked quantifiers, other escapes) with SchemaError rather than read it differently.

Formats: date and date-time as RFC 3339 defines them, ASCII digits only (a leap second only at 23:59:60 UTC on a day
that had one); uri as RFC 3986, parsed into its parts, each from its own characters, with brackets only around an IPv6
or IPvFuture host and a host required for http and https; iri as RFC 3987, which adds ucschar everywhere and iprivate in
the query. Annotations are not checked against the file.

Before anything is checked, the whole schema is read once, every branch, definition and reference target included: any
other keyword, format, type, reference or a keyword value of the wrong shape raises SchemaError, so a schema can never
ask for a check that is silently not made.

`publishable(obj, schema, label)` is the gate a writer calls before it replaces a published file: it reports the
problems (stderr, and a warning annotation on GitHub Actions) and returns False, and the writer then leaves the last good
file in place.
"""
from __future__ import annotations

import datetime as dt
import functools
import ipaddress
import json
import math
import os
import re
import sys
import urllib.parse
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "data/v1/schema"
ANNOTATIONS = {"$schema", "$id", "$defs", "$comment", "title", "description", "examples", "default", "deprecated"}
KEYWORDS = {"type", "const", "enum", "required", "properties", "additionalProperties", "items", "minItems", "maxItems",
            "pattern", "minLength", "maxLength", "format", "minimum", "maximum", "$ref", "allOf", "anyOf", "oneOf", "not",
            "if", "then", "else"}
FORMATS = {"date", "date-time", "uri", "iri"}
TYPES = {"null", "boolean", "integer", "number", "string", "array", "object"}
# RFC 3986 character classes, and the ones RFC 3987 adds for an IRI: ucschar in every component, iprivate in the query
# only. Private-use code points and noncharacters outside those ranges are refused.
_UNRESERVED = "A-Za-z0-9\\-._~"
_SUB_DELIMS = "!$&'()*+,;="
_UCSCHAR = "".join(f"{chr(a)}-{chr(b)}" for a, b in [(0xA0, 0xD7FF), (0xF900, 0xFDCF), (0xFDF0, 0xFFEF),
                                                     *[(p << 16, (p << 16) + 0xFFFD) for p in range(1, 14)], (0xE1000, 0xEFFFD)])
_IPRIVATE = "".join(f"{chr(a)}-{chr(b)}" for a, b in [(0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD)])
_PCT = "%[0-9A-Fa-f]{2}"


def _part(extra: str, iri: bool, private: bool = False) -> re.Pattern:
    """A component made of unreserved, sub-delims, the extra ASCII characters given and percent-escapes (and, for an
    IRI, ucschar; iprivate too in a query)."""
    wide = (_UCSCHAR if iri else "") + (_IPRIVATE if iri and private else "")
    return re.compile(f"(?:[{_UNRESERVED}{re.escape(_SUB_DELIMS)}{re.escape(extra)}{wide}]|{_PCT})*")


_PARTS = {iri: {"userinfo": _part(":", iri), "reg_name": _part("", iri), "path": _part(":@/", iri),
                "query": _part(":@/?", iri, private=True), "fragment": _part(":@/?", iri)} for iri in (False, True)}
_IP_FUTURE = re.compile(f"[vV][0-9A-Fa-f]+\\.[{_UNRESERVED}{re.escape(_SUB_DELIMS)}:]+")
DATE_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:([Zz])|([+-])(\d{2}):(\d{2}))", re.ASCII)
# Every leap second so far (IERS), each at 23:59:60 UTC on these days; none has been announced since 2016.
LEAP_SECOND_DAYS = {"1972-06-30", "1972-12-31", "1973-12-31", "1974-12-31", "1975-12-31", "1976-12-31", "1977-12-31",
                    "1978-12-31", "1979-12-31", "1981-06-30", "1982-06-30", "1983-06-30", "1985-06-30", "1987-12-31",
                    "1989-12-31", "1990-12-31", "1992-06-30", "1993-06-30", "1994-06-30", "1995-12-31", "1997-06-30",
                    "1998-12-31", "2005-12-31", "2008-12-31", "2012-06-30", "2015-06-30", "2016-12-31"}
MAX_PROBLEMS = 50
DIALECT = "https://json-schema.org/draft/2020-12/schema"


class SchemaError(ValueError):
    """The schema uses something this checker does not implement."""


def load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


@functools.lru_cache(maxsize=256)
def _ecma_pattern(p: str) -> str:
    """The Python regex for an ECMA-262 pattern within the subset both engines read alike; $ becomes \\Z, since ECMA-262's $
    does not match before a final newline. Anything outside the subset raises SchemaError."""
    out, i, n = [], 0, len(p)
    simple = "dDwW"
    quantified = lazy = False  # the last token was a quantifier (and a lazy ? after it)
    while i < n:
        c = p[i]
        if c in "*+?{" and quantified and (lazy or c != "?"):
            # ECMA-262 has no possessive (a*+, a{1,2}+) or stacked quantifiers; Python reads some of them.
            raise SchemaError("a quantifier right after a quantifier is not supported")
        if c == "?" and quantified:
            out.append(c)
            lazy = True
            i += 1
            continue
        quantified = lazy = False
        if c in "*+?":
            out.append(c)
            quantified = True
            i += 1
            continue
        if c == "\\":
            e = p[i + 1] if i + 1 < n else ""
            if not e or (e not in simple and e not in "\\^$.|?*+()[]{}/"):  # Unicode mode: syntax characters and / only
                raise SchemaError(f"pattern escape \\{e} is not supported")
            out.append(c + e)
            i += 2
        elif c == "[":
            j = i + 1 + (p[i + 1:i + 2] == "^")
            if p[j:j + 1] in ("]", ""):
                raise SchemaError("an empty or unterminated character class is not supported")
            k = j
            atoms: list[str] = []  # each class atom in order: one character, an escape, or "-"
            while k < n and p[k] != "]":
                if p[k] == "\\":
                    if p[k + 1:k + 2] not in tuple(simple) + tuple("\\]^-[/"):
                        raise SchemaError(f"pattern escape \\{p[k + 1:k + 2]} in a class is not supported")
                    atoms.append(p[k:k + 2])
                    k += 2
                elif p[k] == "[":
                    raise SchemaError("a [ inside a character class is not supported")
                else:
                    atoms.append(p[k])
                    k += 1
            if k >= n:
                raise SchemaError("an unterminated character class")
            # In Unicode mode a range cannot end at \d, \D, \w or \W ([\d-a] is an error there; Python reads - literally).
            # Ranges are read left to right as both parsers read them: x-y takes three atoms, so [a-b-\d] is a-b, -, \d.
            r = 0
            while r < len(atoms):
                if r + 2 < len(atoms) and atoms[r + 1] == "-":
                    if {atoms[r], atoms[r + 2]} & {"\\d", "\\D", "\\w", "\\W"}:
                        raise SchemaError("a range from or to \\d, \\D, \\w or \\W is not supported")
                    r += 3
                else:
                    r += 1
            out.append(p[i:k + 1])
            i = k + 1
        elif c == ".":
            raise SchemaError("'.' reads differently in ECMA-262 and Python; use a character class")
        elif c == "$":
            out.append(r"\Z")
            i += 1
        elif c == "(":
            if p.startswith("(?:", i):
                out.append("(?:")
                i += 3
            elif p[i + 1:i + 2] == "?":
                raise SchemaError("lookarounds, named groups and inline flags are not supported")
            else:
                out.append(c)
                i += 1
        elif c == "{":
            m = re.match(r"\{[0-9]+(?:,[0-9]*)?\}", p[i:])
            if not m:
                raise SchemaError("a { that is not a quantifier is not supported")
            out.append(m.group(0))
            quantified = True
            i += m.end()
        elif c in "}]":
            raise SchemaError(f"a {c} that closes nothing is not supported")
        else:
            out.append(c)
            i += 1
    translated = "".join(out)
    try:
        re.compile(translated, re.ASCII)
    except (re.error, OverflowError, RecursionError, ValueError) as ex:  # a{4294967296} overflows rather than errs
        raise SchemaError(f"pattern does not compile: {type(ex).__name__}: {ex}") from None
    return translated


def _audit(s, root: dict, seen: set[int]) -> None:
    """Read the whole schema once, every branch and definition and the target of every reference, and raise SchemaError
    on anything this checker cannot check or a keyword whose value has the wrong shape, whether or not a given file would
    reach it."""
    if isinstance(s, bool):
        return
    if not isinstance(s, dict):
        raise SchemaError(f"a schema must be an object or a boolean, not {type(s).__name__}")
    if id(s) in seen:
        return
    seen.add(id(s))
    if s is not root and "$id" in s:
        raise SchemaError("a nested $id (a second schema resource) is not supported")
    if "$schema" in s and (s is not root or s["$schema"] != DIALECT):
        raise SchemaError(f"$schema must be {DIALECT}, at the root only")
    if not isinstance(s.get("type", ""), (str, list)):
        raise SchemaError(f"type must be a name or a list of names, not {type(s['type']).__name__}")
    for k, want in {"title": str, "description": str, "$comment": str, "$id": str, "examples": list, "deprecated": bool}.items():
        if k in s and not isinstance(s[k], want):
            raise SchemaError(f"{k} must be a {want.__name__}")
    unknown = set(s) - KEYWORDS - ANNOTATIONS
    if unknown:
        raise SchemaError(f"unsupported keywords {sorted(unknown)}")
    shapes = {"properties": dict, "$defs": dict, "allOf": list, "anyOf": list, "oneOf": list, "required": list, "enum": list,
              "$ref": str, "pattern": str, "format": str, "minimum": (int, float), "maximum": (int, float)}
    for k, want in shapes.items():
        if k in s and (not isinstance(s[k], want) or isinstance(s[k], bool)):
            raise SchemaError(f"{k} has the wrong shape: {type(s[k]).__name__}")
    for k in ("minItems", "maxItems", "minLength", "maxLength"):  # a non-negative integer; 1.0 is an integer in JSON
        if k in s and not (_is(s[k], "integer") and s[k] >= 0):
            raise SchemaError(f"{k} must be a non-negative integer")
    if isinstance(s.get("type"), list) and any(not isinstance(x, str) for x in s["type"]):
        raise SchemaError("a type list must hold names")
    if any(not isinstance(k, str) for k in s.get("required", [])) or len(set(s.get("required", []))) != len(s.get("required", [])):
        raise SchemaError("required must list distinct names")
    for k in ("allOf", "anyOf", "oneOf"):
        if k in s and not s[k]:
            raise SchemaError(f"{k} must not be empty")
    if isinstance(s.get("type"), list) and (not s["type"] or len(set(s["type"])) != len(s["type"])):
        raise SchemaError("a type list must be non-empty and distinct")
    types = s.get("type", [])
    for t in types if isinstance(types, list) else [types]:
        if t not in TYPES:
            raise SchemaError(f"unknown type {t!r}")
    if "format" in s and s["format"] not in FORMATS:
        raise SchemaError(f"unknown format {s['format']!r}")
    if "pattern" in s:
        _ecma_pattern(s["pattern"])
    subs = [*s.get("properties", {}).values(), *s.get("$defs", {}).values(), *s.get("allOf", []), *s.get("anyOf", []),
            *s.get("oneOf", [])]
    subs += [s[k] for k in ("additionalProperties", "items", "not", "if", "then", "else") if k in s]
    if "$ref" in s:
        subs.append(_resolve(root, s["$ref"]))  # its target is a schema too, wherever it sits
    for sub in subs:
        _audit(sub, root, seen)


def _is(v, t: str) -> bool:
    if t == "null":
        return v is None
    if t == "boolean":
        return isinstance(v, bool)
    if t == "integer":
        return not isinstance(v, bool) and (isinstance(v, int) or (isinstance(v, float) and v.is_integer()))
    if t == "number":  # JSON has no NaN or Infinity, so neither is a number (json.loads reads 1e400 as inf)
        return not isinstance(v, bool) and (isinstance(v, int) or (isinstance(v, float) and math.isfinite(v)))
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
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v):
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
        if s < 60:
            return True
        # A leap second is the last second of a UTC day that had one. Shift by the offset in whole days and minutes, with
        # ordinals, so a date at either end of the calendar cannot overflow.
        days, minute = divmod(h * 60 + mi - off, 24 * 60)
        ordinal = dt.date(y, mo, d).toordinal() + days
        if minute != 23 * 60 + 59 or not 1 <= ordinal <= dt.date.max.toordinal():
            return False
        return dt.date.fromordinal(ordinal).isoformat() in LEAP_SECOND_DAYS
    if fmt in ("uri", "iri"):
        return _uri_ok(v, fmt == "iri")
    raise SchemaError(f"unknown format {fmt!r}")


def _uri_ok(v: str, iri: bool) -> bool:
    """RFC 3986 (or, for an IRI, RFC 3987): scheme ":" hier-part ["?" query] ["#" fragment], each part from its own
    characters, brackets only around an IP literal host, and a non-empty host for http and https."""
    parts = _PARTS[iri]
    m = re.match(r"([A-Za-z][A-Za-z0-9+.-]*):", v)
    if not m:
        return False
    scheme, rest = m.group(1).lower(), v[m.end():]
    rest, _, fragment = rest.partition("#") if "#" in rest else (rest, "", None)
    rest, _, query = rest.partition("?") if "?" in rest else (rest, "", None)
    if fragment is not None and not parts["fragment"].fullmatch(fragment):
        return False
    if query is not None and not parts["query"].fullmatch(query):
        return False
    host = None
    if rest.startswith("//"):
        authority, slash, path = rest[2:].partition("/")
        path = slash + path
        userinfo, at, hostport = authority.rpartition("@")
        if at and not parts["userinfo"].fullmatch(userinfo):
            return False
        if hostport.startswith("["):
            literal, close, after = hostport[1:].partition("]")
            if not close or not _ip_literal(literal) or (after and not after.startswith(":")):
                return False
            host, port = literal, after[1:]
        else:
            host, _, port = hostport.partition(":")
            if not parts["reg_name"].fullmatch(host):
                return False
        if not re.fullmatch(r"[0-9]*", port):
            return False
    else:
        path = rest
        if path.startswith("//"):
            return False
    if not parts["path"].fullmatch(path):
        return False
    if scheme in ("http", "https") and not host:
        return False
    return True


def is_iri(v) -> bool:
    """True for a string that is an IRI (RFC 3987), the format of an ingredient's link in the ideas index."""
    return isinstance(v, str) and _uri_ok(v, True)


def _ip_literal(text: str) -> bool:
    """The inside of [ ] in a host: an IPv6 address (no zone id) or an IPvFuture."""
    if _IP_FUTURE.fullmatch(text):
        return True
    if "%" in text:
        return False
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True


def _resolve(root: dict, ref: str) -> dict | bool:
    """A JSON Pointer within the same file (RFC 6901): object members by name, array items by index."""
    if not isinstance(ref, str):
        raise SchemaError(f"$ref must be a string, not {type(ref).__name__}")
    if not ref.startswith("#"):
        raise SchemaError(f"only references within the same file are supported, not {ref!r}")
    if not _PARTS[False]["fragment"].fullmatch(ref[1:]):  # a URI fragment: its own characters, valid percent-escapes only
        raise SchemaError(f"{ref!r} is not a valid URI fragment")
    try:
        pointer = urllib.parse.unquote(ref[1:], errors="strict")  # a URI fragment: percent-decoded first (RFC 6901, 6)
    except UnicodeDecodeError:
        raise SchemaError(f"{ref!r} is not a valid fragment") from None
    if pointer == "":
        return root
    if not pointer.startswith("/"):
        raise SchemaError(f"{ref!r} is not a JSON Pointer")
    node = root
    for part in pointer[1:].split("/"):
        if re.search(r"~(?![01])", part):
            raise SchemaError(f"{ref!r} has an escape other than ~0 and ~1")
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif (isinstance(node, list) and re.fullmatch(r"0|[1-9][0-9]*", part)  # ASCII digits only
              and len(part) <= len(str(len(node))) and int(part) < len(node)):  # length first: int() of 5,000 digits raises
            node = node[int(part)]
        else:
            raise SchemaError(f"{ref!r} points at nothing")
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
        # Read as ECMA-262 reads it (the audit admitted only the subset where Python agrees): \d is 0 to 9, $ is the end.
        if "pattern" in s and not re.search(_ecma_pattern(s["pattern"]), v, re.ASCII):
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
    try:
        return _problems(obj, schema)
    except SchemaError:
        raise
    except RecursionError:
        raise SchemaError("the schema or the file is nested too deeply to check") from None
    except Exception as ex:  # anything else means the check could not be made: never a pass, always a SchemaError
        raise SchemaError(f"the check could not be made: {type(ex).__name__}: {str(ex)[:160]}") from None


def _problems(obj, schema: dict | str) -> list[str]:
    root = load(schema) if isinstance(schema, str) else schema
    try:  # Python's json reads NaN, Infinity and 1e400; none is JSON, and a bound of inf would disable its check
        json.dumps(root, allow_nan=False)
    except (ValueError, TypeError) as ex:
        raise SchemaError(f"the schema is not strict JSON: {ex}") from None
    _audit(root, root, set())
    out: list[str] = []
    try:  # the file must serialise as strict JSON too, or the gate would approve what readers cannot parse
        json.dumps(obj, allow_nan=False)
    except (ValueError, TypeError) as ex:
        out.append(f"$: not strict JSON: {ex}")
    _check(obj, root, root, "$", out)
    return out[:MAX_PROBLEMS]


def publishable(obj, schema: str, label: str) -> bool:
    """True when obj conforms. Otherwise the problems are reported and the caller keeps the last good file."""
    try:
        bad = problems(obj, schema)
    except Exception as ex:  # an unreadable, malformed or unsupported schema is a failed check: never a pass, never a crash
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
