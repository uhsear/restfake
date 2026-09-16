#!/usr/bin/env python
"""A mock ArcGIS REST server that fails the way ArcGIS actually fails.

ArcGIS Server reports failure as HTTP 200 carrying an error envelope in the
body. Every client that checks the status code and not the body treats that as
success. Nobody can write a regression test for it today, because writing one
needs a broken ArcGIS Server, so the code path that handles it is the least
tested path in every GIS script ever written, including this author's own.

This serves /rest/info, a catalog, FeatureServer and MapServer layers, /query
with where, returnCountOnly, returnIdsOnly, resultOffset and resultRecordCount,
plus /generateToken and /addFeatures. That part is ordinary. The product is the
fault flags: a request that comes back 200 with an error body, a token that
stops working in the middle of a paging loop, a page that returns fewer rows
than it promised, a page that repeats the previous page's OBJECTIDs, a layer
that advertises fields it does not return, a slow response and a dropped
connection. Every fault is deterministic, so a failing run replays.

It binds 127.0.0.1 and nothing else. There is no flag that changes that.

    python restfake.py --self-test
    python restfake.py --rows 2500 --max-record-count 1000
    python restfake.py --apply
    python restfake.py --apply --error-after 2 --truncate-page

Exit codes: 0 the plan was printed or the server stopped, 2 the bind failed,
64 usage error.
"""

from __future__ import print_function

import argparse
import html
import http.client
import http.server
import io
import json
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# The address the server binds. This is a constant and not a flag on purpose. A
# fake portal that answers on the LAN is an attractive nuisance: it serves a
# catalog that looks like a real one, it hands out tokens to anybody who asks,
# and the whole point of it is to return wrong answers. Somebody else's script
# finding it and believing it is a failure mode with no upside.
BIND_HOST = "127.0.0.1"

DEFAULT_PORT = 7777

# Rows the fake layer holds, and the page size it caps a query at. The defaults
# are chosen to page: 2500 rows at 1000 is three pages, the last one short.
DEFAULT_ROWS = 2500
DEFAULT_MAX_RECORD_COUNT = 1000

# Version the fake reports. 11.1 is an ArcGIS Enterprise release, so a client
# that gates behaviour on currentVersion takes its modern branch here.
CURRENT_VERSION = 11.1
FULL_VERSION = "11.1.0"

# Spatial reference of the fake geometry. 2881 is NAD83(HARN) / Florida West
# (ftUS), which is what Marion County data actually arrives in.
WKID = 2881

# The token the fake issues. It is a fixed string with no secret in it, so that
# a test can assert on it and a leak of it costs nothing.
FAKE_TOKEN = "FAKE-RESTFAKE-TOKEN-0000000000"

# Parameters whose value never reaches the access log. The password arrives in a
# POST body and the token arrives in the query string, so both are in the exact
# text this server would otherwise echo to stdout.
SECRET_PARAMS = frozenset(["token", "password", "pwd", "client_secret",
                           "refresh_token", "code"])
REDACTED = "[redacted]"

# Seconds a request may take before the handler gives up on reading it.
REQUEST_TIMEOUT = 30

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

# The fake layer's schema. This is the promise. --drop-fields breaks it by
# removing a field from the response while leaving it advertised here, which is
# how a real service behaves after a field is deleted from the underlying table
# and the service is not restarted.
FIELDS = [
    {"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID"},
    {"name": "PARCELID", "type": "esriFieldTypeString", "alias": "Parcel ID",
     "length": 24},
    {"name": "OWNER", "type": "esriFieldTypeString", "alias": "Owner Name",
     "length": 80},
    {"name": "ACRES", "type": "esriFieldTypeDouble", "alias": "Acres"},
    {"name": "STATUS", "type": "esriFieldTypeString", "alias": "Status",
     "length": 16},
    {"name": "LASTEDIT", "type": "esriFieldTypeDate", "alias": "Last Edited",
     "length": 8},
]

OID_FIELD = "OBJECTID"

# Services in the catalog. Both are backed by the same rows, because the fault
# behaviour is the subject here and two different datasets would only be two
# things to keep in your head.
SERVICES = [("Parcels", "FeatureServer"), ("Basemap", "MapServer")]

STATUSES = ("ACTIVE", "PENDING", "EXEMPT", "SPLIT")

# 2026-01-01T00:00:00Z in epoch milliseconds, the base for the LASTEDIT field.
BASE_EPOCH_MS = 1767225600000

# Operators the where parser understands, longest and most specific first so
# that ">=" is never read as ">" and " NOT LIKE " is never read as " LIKE ".
OPERATORS = ("<>", "!=", ">=", "<=", " NOT LIKE ", " LIKE ", " IN ", "=", ">",
             "<")


# ----------------------------------------------------------------- pure core

def make_rows(count):
    """Build the fake table. Deterministic: the same count gives the same rows."""
    if count < 0:
        raise ValueError("--rows cannot be negative, got %r" % (count,))
    rows = []
    for oid in range(1, count + 1):
        rows.append({
            "attributes": {
                "OBJECTID": oid,
                "PARCELID": "%05d-%03d-%02d" % (oid * 7 % 100000, oid % 1000,
                                                oid % 100),
                "OWNER": "OWNER %04d" % oid,
                "ACRES": round(0.25 + (oid % 400) * 0.13, 2),
                "STATUS": STATUSES[oid % len(STATUSES)],
                "LASTEDIT": BASE_EPOCH_MS + oid * 86400000,
            },
            "geometry": {"x": 550000.0 + oid * 3.5, "y": 1620000.0 + oid * 2.25},
        })
    return rows


def make_faults(error_after=None, token_expires_after=None, truncate_page=False,
                duplicate_oids=False, drop_fields=(), slow=0, flaky=0.0):
    """Collect the fault flags, refusing a value that cannot mean anything.

    Every fault is off by default. A spec built with no arguments is an
    ordinary, honest ArcGIS REST endpoint.
    """
    if error_after is not None and error_after < 0:
        raise ValueError("--error-after cannot be negative, got %r"
                         % (error_after,))
    if token_expires_after is not None and token_expires_after < 0:
        raise ValueError("--token-expires-after cannot be negative, got %r"
                         % (token_expires_after,))
    if slow < 0:
        raise ValueError("--slow cannot be negative, got %r" % (slow,))
    # flaky != flaky is true only of NaN. argparse's float() reads the word
    # "nan" happily, and NaN answers False to every < and > comparison, so
    # without this clause --flaky nan passes validation and then kills the
    # first data request with "cannot convert float NaN to integer", thrown
    # from inside the request handler where nothing is left to report it.
    if flaky != flaky or flaky < 0.0 or flaky > 1.0:
        raise ValueError("--flaky is a fraction between 0 and 1, got %r"
                         % (flaky,))
    names = [f.strip() for f in drop_fields if f and f.strip()]
    known = set(f["name"] for f in FIELDS)
    for name in names:
        if name not in known:
            raise ValueError("--drop-fields names %r, which the layer does not "
                             "have. Known fields: %s"
                             % (name, ", ".join(sorted(known))))
    return {
        "error_after": error_after,
        "token_expires_after": token_expires_after,
        "truncate_page": bool(truncate_page),
        "duplicate_oids": bool(duplicate_oids),
        "drop_fields": names,
        "slow": slow,
        "flaky": flaky,
    }


def build_spec(rows=DEFAULT_ROWS, max_record_count=DEFAULT_MAX_RECORD_COUNT,
               faults=None):
    """Everything a response builder needs. No sockets, no state, no clock."""
    if max_record_count < 1:
        raise ValueError("--max-record-count must be at least 1, got %r"
                         % (max_record_count,))
    return {
        "rows": make_rows(rows),
        "max_record_count": max_record_count,
        "faults": faults if faults is not None else make_faults(),
        "token": FAKE_TOKEN,
    }


def armed(spec):
    """Names of the faults this spec has switched on, in flag order."""
    f = spec["faults"]
    out = []
    if f["error_after"] is not None:
        out.append("--error-after %d" % f["error_after"])
    if f["token_expires_after"] is not None:
        out.append("--token-expires-after %d" % f["token_expires_after"])
    if f["truncate_page"]:
        out.append("--truncate-page")
    if f["duplicate_oids"]:
        out.append("--duplicate-oids")
    if f["drop_fields"]:
        out.append("--drop-fields %s" % ",".join(f["drop_fields"]))
    if f["slow"]:
        out.append("--slow %d" % f["slow"])
    if f["flaky"]:
        out.append("--flaky %g" % f["flaky"])
    return out


def error_envelope(code, message, details=()):
    """The body ArcGIS sends with HTTP 200 when the request failed.

    This shape is the whole reason this tool exists. There is no status code to
    check: the response is a 200 and the failure is three keys down in the body.
    """
    return {"error": {"code": code, "message": message,
                      "details": list(details) if details else []}}


def is_error(body):
    """True when a 200 response is actually a failure. What clients forget."""
    return isinstance(body, dict) and isinstance(body.get("error"), dict)


def html_page(path):
    """The Services Directory page a request without f=json really gets.

    A client that forgets f=json does not get an error. It gets HTML, and
    json.loads reports a doctype, which reads like a broken parser rather than
    a missing parameter.
    """
    return ("<html><head><title>ArcGIS REST Services Directory</title></head>"
            "<body><h2>%s</h2><p>HTML because the request did not ask for "
            "f=json.</p></body></html>" % html.escape(path))


def wants_json(params):
    return (params.get("f") or "").lower() in ("json", "pjson")


def rest_path(path):
    """Segments after /rest, or None when the path is not under /rest at all.

    A web adaptor prefix is ignored, so /arcgis/rest/services and /rest/services
    route the same way. Real deployments have the prefix and toy ones do not,
    and a client should not have to care which it is pointed at.
    """
    parts = [s for s in path.split("?", 1)[0].split("/") if s]
    if "rest" not in parts:
        return None
    return parts[parts.index("rest") + 1:]


def counts_toward_faults(path):
    """True for the data requests the fault counters count.

    Metadata and sign-in are not counted. If /rest/info advanced the counter,
    --error-after 2 would mean a different request for every client, depending
    on how many times it read the catalog first.
    """
    parts = rest_path(path)
    return bool(parts) and parts[-1] in ("query", "addFeatures")


def should_flake(n, rate):
    """True when request n is the one that drops.

    Deterministic, not random. A CI harness whose failures land on different
    requests each run cannot pin a regression, and a flaky test suite is the
    thing this tool exists to stop people shipping. int(n*rate) advances exactly
    rate of the time, so --flaky 0.5 drops every second request, forever.
    """
    if rate <= 0.0 or n < 1:
        return False
    return int(n * rate) > int((n - 1) * rate)


# --------------------------------------------------------------- where clause

def _literal(text):
    """Read a SQL literal: a quoted string, a number, or NULL."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1].replace("''", "'")
    if text.upper() == "NULL":
        return None
    try:
        if re.match(r"^[-+]?\d+$", text):
            return int(text)
        return float(text)
    except ValueError:
        raise ValueError("cannot read %r as a value" % (text,))


def _like_pattern(text):
    r"""Turn a SQL LIKE pattern into a regular expression.

    % matches any run, _ matches one character, and everything else is taken
    literally, which is why the rest goes through re.escape.
    """
    out = []
    for ch in text:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("^" + "".join(out) + "$")


def _split_where(text):
    """Return (field, operator, value_text), or None when there is no operator."""
    upper = text.upper()
    for op in OPERATORS:
        i = upper.find(op)
        if i > 0:
            return text[:i].strip(), op.strip().upper(), text[i + len(op):].strip()
    return None


def _compare(op, left, right):
    """Apply one SQL operator, answering False rather than raising on a type mix.

    A where clause comparing a string column with a number is a client bug, and
    a real database answers it with an error. Answering False here keeps the
    fake serving, and the clause that got there was already refused by
    parse_where if it named a field that does not exist.
    """
    if op in ("=",):
        return left == right
    if op in ("<>", "!="):
        return left != right
    if left is None or right is None:
        return False
    if isinstance(left, str) != isinstance(right, str):
        return False
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    return left <= right


def parse_where(where, field_names):
    """Compile a where clause to a predicate over one row's attributes.

    A clause naming a field the layer does not have raises, because that is what
    the real server does, and a fake that quietly matched everything would turn
    a client's typo into a silently full result set.

    ponytail: one comparison per clause, no AND/OR and no parentheses. Splitting
    on " AND " breaks on a string literal containing the word, and a real SQL
    parser is a week of work for a fixture layer. Add one if a client under test
    needs compound clauses.
    """
    text = (where or "").strip()
    if not text or text in ("1=1", "1 = 1"):
        return lambda attrs: True
    if text in ("1=0", "1 = 0"):
        return lambda attrs: False

    split = _split_where(text)
    if split is None:
        raise ValueError("cannot parse the where clause %r" % (where,))
    field, op, value_text = split
    if field not in field_names:
        raise ValueError("where clause names the field %r, which this layer "
                         "does not have" % (field,))

    if op == "IN":
        inner = value_text.strip()
        if not (inner.startswith("(") and inner.endswith(")")):
            raise ValueError("IN needs a parenthesised list, got %r"
                             % (value_text,))
        wanted = [_literal(p) for p in inner[1:-1].split(",") if p.strip()]
        return lambda attrs: attrs.get(field) in wanted

    if op in ("LIKE", "NOT LIKE"):
        value = _literal(value_text)
        if not isinstance(value, str):
            raise ValueError("LIKE needs a quoted pattern, got %r" % (value_text,))
        pattern = _like_pattern(value)
        negate = op == "NOT LIKE"
        def like(attrs):
            got = attrs.get(field)
            hit = isinstance(got, str) and bool(pattern.match(got))
            return (not hit) if negate else hit
        return like

    value = _literal(value_text)
    return lambda attrs: _compare(op, attrs.get(field), value)


def select_rows(spec, where):
    """Rows matching the where clause, in OBJECTID order."""
    names = set(f["name"] for f in FIELDS)
    predicate = parse_where(where, names)
    return [r for r in spec["rows"] if predicate(r["attributes"])]


# -------------------------------------------------------------------- paging

def page_rows(rows, offset, count, truncate=False, duplicate=0):
    """Return (page, exceededTransferLimit) the way ArcGIS computes them.

    exceededTransferLimit is set by a FULL page, not by a remaining count. A
    layer holding exactly 2000 rows at maxRecordCount 1000 therefore hands back
    two full pages both claiming there is more, and a third, empty page. A
    client that stops when the flag is false makes three requests for two pages
    of data. A client that stops when the page is empty makes the same three
    requests and is correct. They are not the same loop and only one of them
    survives a layer whose size is a multiple of the page size.
    """
    start = offset
    if duplicate and offset > 0:
        # A real server does this when a cursor is rebuilt between pages under
        # a concurrent edit: the second page starts before the first one ended.
        start = max(0, offset - max(1, count // 10))
    page = rows[start:start + count]
    exceeded = len(page) == count
    if truncate and exceeded:
        # Only a FULL page is truncated, because a full page is the one that
        # promised something: exceededTransferLimit stays true and says the
        # page was capped, while half the rows are missing. A client advancing
        # by resultRecordCount instead of by len(features) walks straight past
        # the rows it never received. The last, short page is left honest, so
        # a correct loop can still finish and the fault stays a paging bug
        # rather than a layer that cannot be read at all.
        page = page[:max(1, len(page) // 2)]
    return page, exceeded


def page_plan(total, max_record_count):
    """The (offset, size) pairs a correct client would walk. For the plan text."""
    plan = []
    offset = 0
    while True:
        size = min(max_record_count, max(0, total - offset))
        plan.append((offset, size))
        if size < max_record_count:
            return plan
        offset += max_record_count


# ---------------------------------------------------------- response builders

def rest_info_response(spec):
    return {
        "currentVersion": CURRENT_VERSION,
        "fullVersion": FULL_VERSION,
        "owningSystemUrl": "http://%s" % BIND_HOST,
        "authInfo": {
            "isTokenBasedSecurity": spec["faults"]["token_expires_after"] is not None,
            "tokenServicesUrl": "http://%s/rest/generateToken" % BIND_HOST,
            "shortLivedTokenValidity": 60,
        },
    }


def catalog_response(spec):
    return {
        "currentVersion": CURRENT_VERSION,
        "folders": [],
        "services": [{"name": name, "type": kind} for name, kind in SERVICES],
    }


def service_response(spec, name, kind):
    return {
        "currentVersion": CURRENT_VERSION,
        "serviceDescription": "restfake %s, a deliberately unreliable %s"
                              % (name, kind),
        "hasVersionedData": False,
        "supportsDisconnectedEditing": False,
        "maxRecordCount": spec["max_record_count"],
        "capabilities": "Query,Create" if kind == "FeatureServer" else "Map,Query",
        "layers": [{"id": 0, "name": "%s Layer 0" % name,
                    "geometryType": "esriGeometryPoint", "defaultVisibility": True,
                    "minScale": 0, "maxScale": 0}],
        "tables": [],
    }


def layer_response(spec, name, kind, layer_id):
    """The layer definition. This is the advertisement, and --drop-fields does
    not touch it: the promise stays intact while the data stops keeping it."""
    return {
        "currentVersion": CURRENT_VERSION,
        "id": layer_id,
        "name": "%s Layer %d" % (name, layer_id),
        "type": "Feature Layer",
        "geometryType": "esriGeometryPoint",
        "objectIdField": OID_FIELD,
        "uniqueIdField": {"name": OID_FIELD, "isSystemMaintained": True},
        "maxRecordCount": spec["max_record_count"],
        "supportsPagination": True,
        "capabilities": "Query,Create" if kind == "FeatureServer" else "Query",
        "extent": {"xmin": 550000.0, "ymin": 1620000.0,
                   "xmax": 560000.0, "ymax": 1630000.0,
                   "spatialReference": {"wkid": WKID}},
        "fields": [dict(f) for f in FIELDS],
    }


def resolve_out_fields(params, spec):
    """Field names the response should carry, before --drop-fields takes any away."""
    raw = (params.get("outFields") or "*").strip()
    names = [f["name"] for f in FIELDS]
    if raw in ("*", ""):
        return names
    wanted = [p.strip() for p in raw.split(",") if p.strip()]
    for name in wanted:
        if name not in names:
            raise ValueError("outFields names the field %r, which this layer "
                             "does not have" % (name,))
    return wanted


def build_feature(row, out_fields, drop_fields, want_geometry):
    attrs = {}
    for name in out_fields:
        if name in drop_fields:
            continue
        attrs[name] = row["attributes"][name]
    feature = {"attributes": attrs}
    if want_geometry:
        feature["geometry"] = dict(row["geometry"])
    return feature


def _flag(params, name, default=False):
    raw = params.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("true", "1", "yes")


def _int_param(params, name, default):
    raw = params.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        raise ValueError("%s must be an integer, got %r" % (name, raw))


def query_response(params, spec, n=0):
    """Answer /query. Pure: params and spec in, a response body out.

    n is the ordinal of this request among the data requests, which is what the
    counting faults key off. n=0 means "not counted", and every fault that
    counts is inert at n=0.
    """
    faults = spec["faults"]

    if faults["error_after"] is not None and n > faults["error_after"]:
        return error_envelope(
            500, "Unable to complete operation.",
            ["Error performing query operation", "restfake --error-after %d"
             % faults["error_after"]])

    try:
        rows = select_rows(spec, params.get("where"))
        out_fields = resolve_out_fields(params, spec)
        offset = _int_param(params, "resultOffset", 0)
        requested = _int_param(params, "resultRecordCount",
                               spec["max_record_count"])
    except ValueError as exc:
        # The real server answers a bad where clause with a 200 and this body.
        return error_envelope(400, "Invalid or missing input parameters.",
                              [str(exc)])

    if offset < 0:
        return error_envelope(400, "Invalid or missing input parameters.",
                              ["resultOffset cannot be negative"])
    if requested < 0:
        return error_envelope(400, "Invalid or missing input parameters.",
                              ["resultRecordCount cannot be negative"])

    if _flag(params, "returnCountOnly"):
        # Count and ids ignore paging. Asking for a count with resultOffset set
        # gives the count of the whole result set, not of the page, which is
        # how a paging loop that trusts the count decides it is finished early.
        return {"count": len(rows)}

    if _flag(params, "returnIdsOnly"):
        return {"objectIdFieldName": OID_FIELD,
                "uniqueIdField": {"name": OID_FIELD, "isSystemMaintained": True},
                "objectIds": [r["attributes"][OID_FIELD] for r in rows]}

    # A resultRecordCount above maxRecordCount is silently capped. It does not
    # fail, which is how a loop that advances by its own page size loses rows.
    count = min(requested, spec["max_record_count"])
    page, exceeded = page_rows(rows, offset, count,
                               truncate=bool(n) and faults["truncate_page"],
                               duplicate=1 if (n and faults["duplicate_oids"])
                               else 0)

    drop = set(faults["drop_fields"])
    want_geometry = _flag(params, "returnGeometry", True)
    visible = [f for f in FIELDS if f["name"] in out_fields
               and f["name"] not in drop]
    body = {
        "objectIdFieldName": OID_FIELD,
        "uniqueIdField": {"name": OID_FIELD, "isSystemMaintained": True},
        "globalIdFieldName": "",
        "geometryType": "esriGeometryPoint",
        "spatialReference": {"wkid": WKID},
        "fields": [dict(f) for f in visible],
        "features": [build_feature(r, out_fields, drop, want_geometry)
                     for r in page],
        "exceededTransferLimit": exceeded,
    }
    return body


def generate_token_response(params, spec):
    """Issue a token. Any non-empty credential works: this is not an authenticator.

    An empty username or password is refused, so a client can exercise its
    sign-in failure path without needing a real portal to refuse it.
    """
    username = (params.get("username") or "").strip()
    password = params.get("password") or ""
    if not username or not password:
        return error_envelope(400, "Unable to generate token.",
                              ["Invalid username or password."])
    return {"token": spec["token"], "expires": 9999999999999, "ssl": False}


def add_features_response(params, spec, n=0):
    """Answer /addFeatures.

    Under --error-after this returns HTTP 200, no top-level error, and
    per-feature results saying success false. That is how the real server
    reports a failed insert, and it is the nastiest shape in the API: a client
    that checks the status code and then checks for a top-level error still
    sees nothing wrong, and reports that it wrote rows it did not write.
    """
    raw = params.get("features")
    if raw is None:
        return error_envelope(400, "Invalid or missing input parameters.",
                              ["features is required"])
    try:
        features = json.loads(raw)
    except ValueError:
        return error_envelope(400, "Invalid or missing input parameters.",
                              ["features was not valid JSON"])
    if not isinstance(features, list):
        return error_envelope(400, "Invalid or missing input parameters.",
                              ["features must be a JSON list"])

    faults = spec["faults"]
    failing = (faults["error_after"] is not None and n > faults["error_after"])
    next_oid = len(spec["rows"])
    results = []
    for i, _feature in enumerate(features):
        if failing:
            results.append({"success": False,
                            "error": {"code": 1000,
                                      "description": "The row could not be "
                                                     "inserted."}})
        else:
            results.append({"objectId": next_oid + i + 1, "success": True,
                            "globalId": None})
    return {"addResults": results}


def route(path, params, spec, n=0):
    """The whole server, as one pure function. The socket layer only calls this.

    Returns a dict to be sent as JSON, or a string to be sent as text/html.
    Every reply goes out as HTTP 200, including every failure, because that is
    what ArcGIS does and reproducing it is the point.
    """
    parts = rest_path(path)
    tail = [s for s in path.split("?", 1)[0].split("/") if s]

    if tail and tail[-1] == "generateToken":
        if not wants_json(params):
            return html_page(path)
        return generate_token_response(params, spec)

    if not wants_json(params):
        return html_page(path)

    if parts is None:
        return error_envelope(404, "Not Found",
                              ["%s is not under /rest" % path])
    if parts == ["info"]:
        return rest_info_response(spec)
    if parts == ["services"]:
        return catalog_response(spec)

    if not parts or parts[0] != "services":
        return error_envelope(404, "Not Found", ["unknown path %s" % path])

    rest = parts[1:]
    if len(rest) < 2:
        return error_envelope(404, "Not Found", ["unknown path %s" % path])
    name, kind = rest[0], rest[1]
    if (name, kind) not in SERVICES:
        return error_envelope(
            404, "Service not found.",
            ["%s/%s is not in the catalog" % (name, kind)])

    if len(rest) == 2:
        return service_response(spec, name, kind)

    try:
        layer_id = int(rest[2])
    except ValueError:
        return error_envelope(404, "Not Found", ["unknown path %s" % path])
    if layer_id != 0:
        return error_envelope(404, "Not Found",
                              ["layer %d does not exist" % layer_id])

    if len(rest) == 3:
        return layer_response(spec, name, kind, layer_id)

    operation = rest[3]
    if operation not in ("query", "addFeatures"):
        return error_envelope(404, "Not Found",
                              ["unsupported operation %s" % operation])
    if operation == "addFeatures" and kind != "FeatureServer":
        return error_envelope(400, "Operation not supported.",
                              ["addFeatures needs a FeatureServer"])

    expired = check_token(params, spec, n)
    if expired is not None:
        return expired

    if operation == "query":
        return query_response(params, spec, n)
    return add_features_response(params, spec, n)


def check_token(params, spec, n):
    """The error envelope for a missing or dead token, or None to carry on.

    Token security is off until --token-expires-after arms it. With it armed a
    token is required, and the one this server issued stops being accepted after
    that many data requests, in the middle of whatever loop is running.
    """
    limit = spec["faults"]["token_expires_after"]
    if limit is None:
        return None
    token = params.get("token")
    if not token:
        return error_envelope(499, "Token Required", ["Token Required"])
    if token != spec["token"]:
        return error_envelope(498, "Invalid Token", ["Invalid token."])
    if n > limit:
        return error_envelope(498, "Invalid Token",
                              ["Token expired.",
                               "restfake --token-expires-after %d" % limit])
    return None


# ------------------------------------------------------------------- logging

def redact_query(query):
    """A query string or POST body with every credential value taken out.

    This runs on the way to the access log, which is the only place this server
    writes anything a person reads. The token travels in the query string and
    the password travels in the POST body, so both are in the exact text that
    would otherwise be echoed.
    """
    if not query:
        return ""
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    out = []
    for key, value in pairs:
        if key.lower() in SECRET_PARAMS:
            value = REDACTED
        out.append("%s=%s" % (key, value))
    return "&".join(out)


def log_line(method, path, query, n, note):
    """One access log line. Credentials are already gone by the time it is built."""
    where = "%s?%s" % (path, redact_query(query)) if query else path
    stamp = "#%d" % n if n else "  "
    return "%s %s %s %s" % (stamp, method, where, note)


def reply_note(body):
    """What the log says happened, which for an error envelope is the truth."""
    if isinstance(body, str):
        return "200 text/html"
    if is_error(body):
        return "200 ERROR %s %s" % (body["error"].get("code"),
                                    body["error"].get("message"))
    if "features" in body:
        return "200 %d feature(s)%s" % (
            len(body["features"]),
            " more" if body.get("exceededTransferLimit") else "")
    if "addResults" in body:
        ok = len([r for r in body["addResults"] if r.get("success")])
        return "200 %d/%d added" % (ok, len(body["addResults"]))
    if "token" in body:
        return "200 token issued"
    return "200 ok"


# --------------------------------------------------------------- socket layer

class _Handler(http.server.BaseHTTPRequestHandler):
    """A thin shell over route(). It owns the counter, the clock and the socket."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        path, _sep, query = self.path.partition("?")
        self._answer("GET", path, query)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        path, _sep, query = self.path.partition("?")
        # A POST carries its parameters in the body, and ArcGIS accepts both at
        # once, so the two are merged before routing.
        self._answer("POST", path, "&".join([p for p in (query, body) if p]))

    def _answer(self, method, path, query):
        server = self.server
        n = 0
        if counts_toward_faults(path):
            with server.lock:
                server.requests += 1
                n = server.requests

        if n and should_flake(n, server.spec["faults"]["flaky"]):
            server.echo(log_line(method, path, query, n,
                                 "DROPPED (--flaky %g)"
                                 % server.spec["faults"]["flaky"]))
            self.close_connection = True
            return

        slow = server.spec["faults"]["slow"]
        if slow:
            time.sleep(slow / 1000.0)

        params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        body = route(path, params, server.spec, n)
        server.echo(log_line(method, path, query, n, reply_note(body)))

        if isinstance(body, str):
            payload = body.encode("utf-8")
            content_type = "text/html; charset=utf-8"
        else:
            payload = json.dumps(body).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        """Silence the default stderr log. Everything goes through echo, which
        redacts; the default one prints self.path with the token still in it."""


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    timeout = REQUEST_TIMEOUT


def _stdout_echo(line):
    """Print an access log line and flush it.

    The flush is not decoration. Redirect this server's output to a file and
    Python line-buffers it only for a terminal: against a file it fills a 8KB
    block first, so `restfake.py --apply > server.log` stayed empty through a
    whole CI run and the log was lost entirely when the process was killed,
    which is how a harness server always ends.
    """
    print(line)
    sys.stdout.flush()


def build_server(spec, port, echo):
    """Bind the socket. BIND_HOST is a constant: nothing here takes a host."""
    server = _Server((BIND_HOST, port), _Handler)
    server.spec = spec
    server.echo = echo
    server.requests = 0
    server.lock = threading.Lock()
    return server


# ---------------------------------------------------------------- plan output

def plan_lines(spec, port):
    """What the tool prints when it has not been told to open a socket."""
    total = len(spec["rows"])
    plan = page_plan(total, spec["max_record_count"])
    base = "http://%s:%d" % (BIND_HOST, port)
    lines = [
        "bind:   %s:%d  (loopback only, there is no flag that changes it)"
        % (BIND_HOST, port),
        "layer:  %d row(s), maxRecordCount %d  ->  %d page(s) for a full read"
        % (total, spec["max_record_count"], len(plan)),
        "fields: %s" % ", ".join(f["name"] for f in FIELDS),
    ]
    faults = armed(spec)
    if faults:
        lines.append("faults: %s" % "  ".join(faults))
    else:
        lines.append("faults: none armed. This is an honest server until you "
                     "arm one.")
    lines.append("")
    lines.append("routes:")
    lines.append("  %s/rest/info?f=json" % base)
    lines.append("  %s/rest/services?f=json" % base)
    for name, kind in SERVICES:
        lines.append("  %s/rest/services/%s/%s/0?f=json" % (base, name, kind))
    lines.append("  %s/rest/services/Parcels/FeatureServer/0/query"
                 "?where=1%%3D1&outFields=*&f=json" % base)
    lines.append("  %s/rest/generateToken (POST username, password)" % base)
    return lines


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the response builders, then over a real loopback socket."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def refuses(argv, label):
        """argparse writes its usage text to stderr, swallowed here so a passing
        self-test prints only PASS lines."""
        noise, sys.stderr = sys.stderr, io.StringIO()
        try:
            _parse(argv)
        except SystemExit:
            check(True, label)
        else:
            check(False, "%s (argparse accepted it)" % label)
        finally:
            sys.stderr = noise

    def captured(fn):
        """Run fn with stdout collected, and give back what it printed."""
        quiet, sys.stdout = sys.stdout, io.StringIO()
        try:
            result = fn()
            return result, sys.stdout.getvalue()
        finally:
            sys.stdout = quiet

    def exits(argv):
        """main()'s exit code, with the banner and the usage message it writes
        swallowed, so a passing self-test prints only PASS lines."""
        quiet, sys.stdout = sys.stdout, io.StringIO()
        noise, sys.stderr = sys.stderr, io.StringIO()
        try:
            return main(argv)
        finally:
            sys.stdout, sys.stderr = quiet, noise

    class _FlushCounter(io.StringIO):
        """A stdout that remembers being flushed. StringIO cannot tell you."""

        flushes = 0

        def flush(self):
            self.flushes += 1

    print("restfake self-test: no portal, no network beyond loopback, no "
          "credentials")
    print("-" * 68)

    spec = build_spec(rows=2500, max_record_count=1000)

    # ---- the error envelope, which is the whole subject
    env = error_envelope(498, "Invalid Token", ["Token expired."])
    check(is_error(env), "an error envelope is recognised as an error")
    check(env["error"]["code"] == 498, "the envelope carries the arcgis code")
    check(env["error"]["message"] == "Invalid Token", "the envelope carries a message")
    check(env["error"]["details"] == ["Token expired."], "the envelope carries details")
    check(error_envelope(400, "bad")["error"]["details"] == [],
          "an envelope with no details still has the details key, because a "
          "client that indexes it must not get a KeyError instead of the error")
    check(set(env.keys()) == set(["error"]),
          "an error body holds nothing but the error, so a client reading "
          "features gets a KeyError and not an empty result set")
    check(is_error({"features": []}) is False,
          "an empty result set is not an error")
    check(is_error({"error": "boom"}) is False,
          "a string under the error key is not an envelope, because arcgis "
          "always sends an object and a bare string means something else broke")
    check(is_error("some html") is False, "an html page is not an error envelope")
    check(is_error({"addResults": [{"success": False}]}) is False,
          "a failed addFeatures carries NO top-level error  <-- pinned defect")

    # ---- the fixture rows
    check(len(spec["rows"]) == 2500, "the fake layer holds the rows asked for")
    check(spec["rows"][0]["attributes"]["OBJECTID"] == 1,
          "objectids start at 1, the way a geodatabase numbers them")
    check(spec["rows"][-1]["attributes"]["OBJECTID"] == 2500,
          "the last objectid equals the row count")
    check(len(set(r["attributes"]["OBJECTID"] for r in spec["rows"])) == 2500,
          "every objectid in the fixture is unique")
    check(make_rows(5) == make_rows(5),
          "the same row count builds byte-identical rows, so a failing test "
          "replays")
    check(make_rows(0) == [], "a zero row layer is allowed and is empty")
    check(sorted(spec["rows"][0]["attributes"].keys())
          == sorted(f["name"] for f in FIELDS),
          "every advertised field is present on every row")
    check(spec["rows"][0]["geometry"]["x"] > 0,
          "a row carries a point geometry")
    raises(lambda: make_rows(-1), "a negative row count raises")
    raises(lambda: build_spec(max_record_count=0),
           "a maxRecordCount of zero raises, because a page size of zero pages "
           "forever")

    # ---- the where clause
    check(len(select_rows(spec, None)) == 2500, "no where clause matches everything")
    check(len(select_rows(spec, "")) == 2500, "an empty where clause matches everything")
    check(len(select_rows(spec, "1=1")) == 2500, "1=1 matches everything")
    check(len(select_rows(spec, "1=0")) == 0, "1=0 matches nothing")
    check(len(select_rows(spec, "OBJECTID = 7")) == 1,
          "an equality on the oid matches one row")
    check(select_rows(spec, "OBJECTID = 7")[0]["attributes"]["OBJECTID"] == 7,
          "and it is the right row")
    check(len(select_rows(spec, "OBJECTID > 2495")) == 5,
          "a greater-than matches the rows above it")
    check(len(select_rows(spec, "OBJECTID >= 2495")) == 6,
          "greater-or-equal includes the boundary row")
    check(len(select_rows(spec, "OBJECTID < 4")) == 3,
          "a less-than matches the rows below it")
    check(len(select_rows(spec, "OBJECTID <= 4")) == 4,
          "less-or-equal includes the boundary row")
    check(len(select_rows(spec, "OBJECTID <> 7")) == 2499,
          "not-equal matches everything else")
    check(len(select_rows(spec, "OBJECTID != 7")) == 2499,
          "!= is read the same as <>")
    check(len(select_rows(spec, "STATUS = 'ACTIVE'")) == 625,
          "a string equality filters on a quoted literal")
    check(len(select_rows(spec, "STATUS IN ('ACTIVE','SPLIT')")) == 1250,
          "IN matches any value in the list")
    check(len(select_rows(spec, "OWNER LIKE 'OWNER 000%'")) == 9,
          "LIKE with a trailing wildcard matches a prefix")
    check(len(select_rows(spec, "OWNER LIKE 'OWNER 0001'")) == 1,
          "LIKE with no wildcard is an equality")
    check(len(select_rows(spec, "OWNER LIKE 'OWNER 000_'")) == 9,
          "an underscore in LIKE matches exactly one character")
    check(len(select_rows(spec, "OWNER LIKE 'OWNER _'")) == 0,
          "and matches ONE and not a run, so a pattern three characters short "
          "of every value matches nothing rather than all 2500")
    check(len(select_rows(spec, "OWNER NOT LIKE 'OWNER 000%'")) == 2491,
          "NOT LIKE is the complement of LIKE")
    check(len(select_rows(spec, "OWNER LIKE 'owner 000%'")) == 0,
          "LIKE is case sensitive here, which SQL Server is not. A clause that "
          "works against a real service can return nothing against this one")
    check(len(select_rows(spec, "  OBJECTID   =   7  ")) == 1,
          "whitespace around the operator is ignored")
    check(len(select_rows(spec, "ACRES > 1000")) == 0,
          "a comparison matching nothing gives an empty list, not an error")
    check(len(select_rows(spec, "OWNER = NULL")) == 0,
          "a comparison against NULL matches nothing, because SQL NULL is not "
          "equal to anything, not even itself")
    check(len(select_rows(spec, "OWNER > NULL")) == 0,
          "and an ordered comparison against NULL matches nothing either, "
          "rather than raising on the type mix")
    check(len(select_rows(spec, "OWNER > 5")) == 0,
          "comparing a string column with a number matches nothing rather than "
          "raising, so one bad clause does not take the server down")
    raises(lambda: select_rows(spec, "BOGUS = 1"),
           "a where clause naming a field the layer does not have raises")
    raises(lambda: select_rows(spec, "OBJECTID"),
           "a where clause with no operator raises")
    raises(lambda: select_rows(spec, "STATUS IN 'ACTIVE'"),
           "IN without a parenthesised list raises")
    raises(lambda: select_rows(spec, "OWNER LIKE 5"),
           "LIKE without a quoted pattern raises")
    raises(lambda: select_rows(spec, "OBJECTID = seven"),
           "an unquoted, non-numeric literal raises")

    # ---- paging, the headline: 2500 rows at 1000 is three pages
    pages = []
    offset = 0
    while True:
        body = query_response({"where": "1=1", "resultOffset": str(offset),
                               "resultRecordCount": "1000", "f": "json"}, spec)
        pages.append(body)
        got = len(body["features"])
        if not body["exceededTransferLimit"]:
            break
        offset += got
        if len(pages) > 10:
            break
    check(len(pages) == 3, "2500 rows at maxRecordCount 1000 is three pages")
    check([len(p["features"]) for p in pages] == [1000, 1000, 500],
          "the three pages hold 1000, 1000 and 500 features")
    check([p["exceededTransferLimit"] for p in pages] == [True, True, False],
          "exceededTransferLimit reads true, true, false")
    walked = [f["attributes"]["OBJECTID"] for p in pages for f in p["features"]]
    check(len(walked) == 2500, "the walk read every row exactly once")
    check(len(set(walked)) == 2500, "and not one objectid came back twice")
    check(walked == list(range(1, 2501)),
          "the objectids arrive in order, with no gap")

    check(page_rows(list(range(10)), 0, 4)[1] is True,
          "a full page says there may be more")
    check(page_rows(list(range(10)), 8, 4)[1] is False,
          "a short page says there is no more")
    check(page_rows(list(range(8)), 4, 4)[1] is True,
          "a layer whose size is a multiple of the page size hands back a FULL "
          "last page claiming there is more  <-- pinned defect")
    check(page_rows(list(range(8)), 8, 4) == ([], False),
          "and the page after it is empty, which is the only honest stop "
          "condition  <-- pinned defect")
    check(page_rows([], 0, 4) == ([], False),
          "an empty layer is one empty page, not an error")

    # ---- resultOffset past the end
    body = query_response({"where": "1=1", "resultOffset": "9000", "f": "json"},
                          spec)
    check(is_error(body) is False,
          "an offset past the end is not an error  <-- pinned defect")
    check(body["features"] == [],
          "an offset past the end returns an empty features array")
    check(body["exceededTransferLimit"] is False,
          "and does not claim there is more to come")
    check("fields" in body,
          "an empty page still carries the field list, so a client building a "
          "schema from the first page does not crash on the last one")
    body = query_response({"where": "1=1", "resultOffset": "2500", "f": "json"},
                          spec)
    check(body["features"] == [],
          "an offset of exactly the row count returns an empty page")
    body = query_response({"where": "1=1", "resultOffset": "-1", "f": "json"},
                          spec)
    check(is_error(body), "a negative resultOffset comes back as an error")
    check(body["error"]["code"] == 400, "and the code is 400")
    body = query_response({"where": "1=1", "resultOffset": "abc", "f": "json"},
                          spec)
    check(is_error(body), "a non-integer resultOffset comes back as an error")
    body = query_response({"where": "1=1", "resultRecordCount": "-5", "f": "json"},
                          spec)
    check(is_error(body), "a negative resultRecordCount comes back as an error")

    # ---- the silent cap on resultRecordCount
    body = query_response({"where": "1=1", "resultRecordCount": "5000",
                           "f": "json"}, spec)
    check(len(body["features"]) == 1000,
          "asking for 5000 rows at maxRecordCount 1000 silently gives 1000 and "
          "does not fail  <-- pinned defect")
    check(body["exceededTransferLimit"] is True,
          "and the capped page still says there is more")
    body = query_response({"where": "1=1", "resultRecordCount": "10", "f": "json"},
                          spec)
    check(len(body["features"]) == 10, "a page smaller than the cap is honoured")
    body = query_response({"where": "1=1", "f": "json"}, spec)
    check(len(body["features"]) == 1000,
          "a query with no resultRecordCount gets maxRecordCount rows")

    # ---- returnCountOnly and returnIdsOnly
    body = query_response({"where": "1=1", "returnCountOnly": "true", "f": "json"},
                          spec)
    check(body == {"count": 2500},
          "returnCountOnly answers with the count and nothing else")
    body = query_response({"where": "STATUS = 'ACTIVE'", "returnCountOnly": "true",
                           "f": "json"}, spec)
    check(body["count"] == 625, "the count honours the where clause")
    body = query_response({"where": "1=1", "returnCountOnly": "true",
                           "resultOffset": "2000", "f": "json"}, spec)
    check(body["count"] == 2500,
          "returnCountOnly ignores resultOffset and counts the whole result "
          "set  <-- pinned defect")
    body = query_response({"where": "1=1", "returnIdsOnly": "true", "f": "json"},
                          spec)
    check(sorted(body.keys()) == ["objectIdFieldName", "objectIds",
                                  "uniqueIdField"],
          "returnIdsOnly answers with the ids and the id field name")
    check(body["objectIdFieldName"] == "OBJECTID",
          "and names the objectid field, which a client needs to dedupe")
    check(len(body["objectIds"]) == 2500,
          "returnIdsOnly ignores maxRecordCount and returns every id  "
          "<-- pinned defect")
    check("features" not in body, "an ids-only response carries no features")
    body = query_response({"where": "OBJECTID <= 3", "returnIdsOnly": "true",
                           "f": "json"}, spec)
    check(body["objectIds"] == [1, 2, 3], "the id list honours the where clause")
    body = query_response({"where": "1=1", "returnCountOnly": "true",
                           "returnIdsOnly": "true", "f": "json"}, spec)
    check(body == {"count": 2500},
          "returnCountOnly wins when both are asked for, the way the real "
          "server resolves it")

    # ---- outFields and geometry
    body = query_response({"where": "OBJECTID = 1", "outFields": "OBJECTID,OWNER",
                           "f": "json"}, spec)
    check(sorted(body["features"][0]["attributes"].keys()) == ["OBJECTID", "OWNER"],
          "outFields narrows the attributes to what was asked for")
    check([f["name"] for f in body["fields"]] == ["OBJECTID", "OWNER"],
          "and the fields block matches what came back")
    body = query_response({"where": "OBJECTID = 1", "outFields": "*", "f": "json"},
                          spec)
    check(len(body["features"][0]["attributes"]) == len(FIELDS),
          "outFields=* returns every field")
    body = query_response({"where": "OBJECTID = 1", "f": "json"}, spec)
    check(len(body["features"][0]["attributes"]) == len(FIELDS),
          "no outFields is the same as outFields=*")
    check("geometry" in body["features"][0],
          "a feature carries its geometry by default")
    body = query_response({"where": "OBJECTID = 1", "returnGeometry": "false",
                           "f": "json"}, spec)
    check("geometry" not in body["features"][0],
          "returnGeometry=false drops the geometry")
    check(body["spatialReference"]["wkid"] == 2881,
          "the response is in EPSG:2881, the Florida West state plane that "
          "Marion County data actually arrives in, and not the 4326 a client "
          "written against a web map would assume")
    body = query_response({"where": "1=1", "outFields": "NOPE", "f": "json"}, spec)
    check(is_error(body), "an unknown outFields name comes back as an error")
    check("NOPE" in body["error"]["details"][0], "and the error names the field")

    # ---- the catalog and the definitions
    info = rest_info_response(spec)
    check(info["currentVersion"] >= 10.0,
          "/rest/info reports a version at or above 10.0, so a client that "
          "gates a modern code path on currentVersion takes it and not the "
          "9.x fallback nobody has exercised in a decade")
    check(info["fullVersion"].startswith("%s." % info["currentVersion"]),
          "and fullVersion agrees with currentVersion, the way a real portal's "
          "two version fields do")
    check(info["authInfo"]["isTokenBasedSecurity"] is False,
          "token security is off until --token-expires-after arms it")
    check(rest_info_response(
        build_spec(rows=1, faults=make_faults(token_expires_after=2))
    )["authInfo"]["isTokenBasedSecurity"] is True,
          "and on once it is armed, so a client picks the sign-in branch")
    cat = catalog_response(spec)
    check([s["name"] for s in cat["services"]] == ["Parcels", "Basemap"],
          "the catalog lists both services")
    check([s["type"] for s in cat["services"]] == ["FeatureServer", "MapServer"],
          "one FeatureServer and one MapServer")
    svc = service_response(spec, "Parcels", "FeatureServer")
    check(svc["maxRecordCount"] == 1000, "the service reports its page size")
    check(svc["layers"][0]["id"] == 0, "the service lists layer 0")
    check("Create" in svc["capabilities"],
          "a FeatureServer advertises Create, so addFeatures is plausible")
    check("Create" not in service_response(spec, "Basemap",
                                           "MapServer")["capabilities"],
          "a MapServer does not")
    lyr = layer_response(spec, "Parcels", "FeatureServer", 0)
    check(lyr["objectIdField"] == "OBJECTID", "the layer names its oid field")
    check([f["name"] for f in lyr["fields"]] == [f["name"] for f in FIELDS],
          "the layer advertises every field")
    check(lyr["maxRecordCount"] == 1000, "the layer reports its page size")
    check(lyr["supportsPagination"] is True, "and says it supports pagination")
    check(lyr["extent"]["spatialReference"]["wkid"] == 2881,
          "the layer extent is in EPSG:2881 too, so a client reprojecting from "
          "the definition and a client reprojecting from the response agree")

    # ---- /generateToken
    body = generate_token_response({"username": "gis_admin",
                                    "password": "anything"}, spec)
    check(body["token"] == "FAKE-RESTFAKE-TOKEN-0000000000",
          "a sign-in returns the one fixed token, whose whole value is in the "
          "source file, so a test can assert on it and a leak of it costs "
          "nothing")
    check("FAKE" in body["token"],
          "and it says so in its own text, because a token in somebody's "
          "clipboard has to be recognisable as this one")
    check(body["expires"] > 0, "and an expiry")
    check(is_error(generate_token_response({"username": "gis_admin",
                                            "password": ""}, spec)),
          "an empty password is refused, so a client can exercise its sign-in "
          "failure path")
    check(is_error(generate_token_response({"password": "x"}, spec)),
          "a missing username is refused")
    check(generate_token_response({"username": "u", "password": ""},
                                  spec)["error"]["code"] == 400,
          "and the refusal is a 400 inside a 200, not an http 401")
    check("password" not in json.dumps(
        generate_token_response({"username": "u", "password": "hunter2"}, spec)),
          "the password is not echoed back in the token response")
    check("hunter2" not in json.dumps(
        generate_token_response({"username": "u", "password": "hunter2"}, spec)),
          "and neither is its value  <-- pinned defect")

    # ---- /addFeatures
    payload = json.dumps([{"attributes": {"OWNER": "NEW"}},
                          {"attributes": {"OWNER": "ALSO NEW"}}])
    body = add_features_response({"features": payload}, spec)
    check(len(body["addResults"]) == 2, "addFeatures answers once per feature")
    check(all(r["success"] for r in body["addResults"]),
          "and reports success when nothing is armed")
    check(body["addResults"][0]["objectId"] == 2501,
          "the new objectid follows the last one in the layer")
    check(len(spec["rows"]) == 2500,
          "the fake does not actually keep the row: it is a harness, not a "
          "database")
    check(is_error(add_features_response({}, spec)),
          "addFeatures with no features is an error")
    check(is_error(add_features_response({"features": "{not json"}, spec)),
          "addFeatures with unparseable json is an error")
    check(is_error(add_features_response({"features": '{"a": 1}'}, spec)),
          "addFeatures with a json object instead of a list is an error")
    check(add_features_response({"features": "[]"}, spec)["addResults"] == [],
          "adding nothing succeeds with no results")

    # ---- FAULT: --error-after
    faulty = build_spec(rows=2500, faults=make_faults(error_after=2))
    q = {"where": "1=1", "f": "json"}
    check(is_error(query_response(q, faulty, 1)) is False,
          "--error-after 2 answers the first request normally")
    check(is_error(query_response(q, faulty, 2)) is False,
          "and the second")
    check(is_error(query_response(q, faulty, 3)),
          "and the third comes back an error  <-- pinned defect")
    check(is_error(query_response(q, faulty, 9)), "and every one after it")
    body = query_response(q, faulty, 3)
    check("features" not in body,
          "the failing response has no features key at all, so a client reading "
          "response['features'] gets a KeyError and not an empty page")
    check(body["error"]["code"] == 500, "the error envelope carries code 500")
    check(any("error-after" in d for d in body["error"]["details"]),
          "and the details name the flag that caused it, so a confused "
          "operator can find it")
    check(is_error(query_response(q, faulty, 0)) is False,
          "an uncounted request is never the one that fails")
    always = build_spec(rows=10, faults=make_faults(error_after=0))
    check(is_error(query_response(q, always, 1)),
          "--error-after 0 fails from the very first data request")
    body = add_features_response({"features": payload}, faulty, 3)
    check(is_error(body) is False,
          "a failing addFeatures carries NO top-level error  <-- pinned defect")
    check(all(r["success"] is False for r in body["addResults"]),
          "the failure is per feature, inside a 200, inside a body that looks "
          "fine at the top level  <-- pinned defect")
    check(all("error" in r for r in body["addResults"]),
          "each failed result carries its own error object")
    check(all("objectId" not in r for r in body["addResults"]),
          "and no objectId, so a client collecting written ids gets nothing "
          "and must notice")

    # ---- FAULT: --token-expires-after
    expiring = build_spec(rows=2500, faults=make_faults(token_expires_after=2))
    path = "/rest/services/Parcels/FeatureServer/0/query"
    good = {"where": "1=1", "f": "json", "token": FAKE_TOKEN}
    check(is_error(route(path, good, expiring, 1)) is False,
          "the issued token works for the first request")
    check(is_error(route(path, good, expiring, 2)) is False, "and the second")
    body = route(path, good, expiring, 3)
    check(is_error(body),
          "and stops being accepted on the third, mid-loop  <-- pinned defect")
    check(body["error"]["code"] == 498,
          "an expired token is arcgis error 498, inside a 200")
    check(any("expired" in d.lower() for d in body["error"]["details"]),
          "and the details say it expired")
    body = route(path, {"where": "1=1", "f": "json"}, expiring, 1)
    check(is_error(body) and body["error"]["code"] == 499,
          "with expiry armed, a request carrying no token is 499 Token Required")
    body = route(path, {"where": "1=1", "f": "json", "token": "WRONG"},
                 expiring, 1)
    check(is_error(body) and body["error"]["code"] == 498,
          "a token this server never issued is refused from the first request")
    check(is_error(route("/rest/info", {"f": "json"}, expiring, 0)) is False,
          "metadata is still readable with no token, the way a real portal "
          "leaves /rest/info open")
    check(is_error(route(path, {"where": "1=1", "f": "json"},
                         build_spec(rows=5), 1)) is False,
          "with expiry not armed, no token is needed at all")
    both = build_spec(rows=10, faults=make_faults(error_after=1,
                                                  token_expires_after=5))
    body = route(path, good, both, 2)
    check(body["error"]["code"] == 500,
          "when both faults could fire, the token is checked first and the "
          "request that had a valid token fails with the query error")

    # ---- FAULT: --truncate-page
    short = build_spec(rows=2500, faults=make_faults(truncate_page=True))
    body = query_response({"where": "1=1", "resultRecordCount": "1000",
                           "f": "json"}, short, 1)
    check(len(body["features"]) == 500,
          "--truncate-page returns half the rows it was asked for  "
          "<-- pinned defect")
    check(body["exceededTransferLimit"] is True,
          "while still claiming a full page, which is what makes it a trap")
    check(len(query_response({"where": "1=1", "resultRecordCount": "1000",
                              "f": "json"}, short, 0)["features"]) == 1000,
          "an uncounted request is not truncated")
    check(len(query_response({"where": "1=1", "resultOffset": "2000",
                              "resultRecordCount": "1000", "f": "json"},
                             short, 1)["features"]) == 500,
          "the last, short page is left honest, so the layer can still be read "
          "to the end and the fault stays a paging bug")
    # A client advancing by its own page size instead of by what it received.
    seen, offset, hops = [], 0, 0
    while hops < 10:
        hops += 1
        body = query_response({"where": "1=1", "resultOffset": str(offset),
                               "resultRecordCount": "1000", "f": "json"},
                              short, hops)
        seen.extend(f["attributes"]["OBJECTID"] for f in body["features"])
        if not body["exceededTransferLimit"]:
            break
        offset += 1000                       # the bug: advance by what we ASKED
    check(len(seen) < 2500,
          "a loop advancing by resultRecordCount loses rows to a short page, "
          "which is exactly the bug this flag exists to catch")
    seen, offset, hops = [], 0, 0
    while hops < 20:
        hops += 1
        body = query_response({"where": "1=1", "resultOffset": str(offset),
                               "resultRecordCount": "1000", "f": "json"},
                              short, hops)
        got = len(body["features"])
        seen.extend(f["attributes"]["OBJECTID"] for f in body["features"])
        if not body["exceededTransferLimit"] or got == 0:
            break
        offset += got                        # the fix: advance by what we GOT
    check(sorted(seen) == list(range(1, 2501)),
          "a loop advancing by len(features) still reads all 2500 rows through "
          "a truncating server")

    # ---- FAULT: --duplicate-oids
    dupes = build_spec(rows=2500, faults=make_faults(duplicate_oids=True))
    first = query_response({"where": "1=1", "resultOffset": "0",
                            "resultRecordCount": "1000", "f": "json"}, dupes, 1)
    second = query_response({"where": "1=1", "resultOffset": "1000",
                             "resultRecordCount": "1000", "f": "json"}, dupes, 2)
    ids1 = [f["attributes"]["OBJECTID"] for f in first["features"]]
    ids2 = [f["attributes"]["OBJECTID"] for f in second["features"]]
    check(len(set(ids1) & set(ids2)) == 100,
          "--duplicate-oids makes the second page repeat 100 oids from the "
          "first  <-- pinned defect")
    check(len(ids2) == 1000,
          "while still handing back a full page, so the count looks right")
    check(max(ids1) == 1000 and min(ids2) == 901,
          "the second page starts before the first one ended")
    check(len(set(ids1) & set(
        [f["attributes"]["OBJECTID"] for f in query_response(
            {"where": "1=1", "resultOffset": "1000",
             "resultRecordCount": "1000", "f": "json"}, spec, 2)["features"]]
    )) == 0, "and a clean server repeats nothing")
    check(page_rows(list(range(100)), 0, 10, duplicate=1)[0] == list(range(10)),
          "the first page is never shifted, because there is nothing before it")

    # ---- FAULT: --drop-fields
    dropped = build_spec(rows=10, faults=make_faults(drop_fields=["OWNER",
                                                                  "ACRES"]))
    body = query_response({"where": "1=1", "f": "json"}, dropped, 1)
    attrs = body["features"][0]["attributes"]
    check("OWNER" not in attrs and "ACRES" not in attrs,
          "--drop-fields removes the fields from the features  <-- pinned defect")
    check("PARCELID" in attrs and "OBJECTID" in attrs,
          "and leaves the rest alone")
    check([f["name"] for f in body["fields"]] == ["OBJECTID", "PARCELID",
                                                  "STATUS", "LASTEDIT"],
          "the query's own fields block matches what it actually sent")
    advert = layer_response(dropped, "Parcels", "FeatureServer", 0)
    check([f["name"] for f in advert["fields"]] == [f["name"] for f in FIELDS],
          "but the LAYER DEFINITION still advertises all six  <-- pinned defect")
    check("OWNER" in [f["name"] for f in advert["fields"]],
          "including the dropped one, which is the whole point: the promise "
          "outlives the data")
    body = query_response({"where": "1=1", "outFields": "OBJECTID,OWNER",
                           "f": "json"}, dropped, 1)
    check(list(body["features"][0]["attributes"].keys()) == ["OBJECTID"],
          "asking for a dropped field by name still does not get it back")
    check(len(select_rows(dropped, "OWNER = 'OWNER 0001'")) == 1,
          "the where clause still filters on a dropped field, because the "
          "column exists and only the response is missing it")
    raises(lambda: make_faults(drop_fields=["NOPE"]),
           "--drop-fields naming a field the layer does not have raises")
    check(make_faults(drop_fields=["", "  ", "OWNER"])["drop_fields"] == ["OWNER"],
          "an empty entry in --drop-fields is ignored, so a trailing comma is "
          "not an error")

    # ---- FAULT: --flaky, which is deterministic
    check(should_flake(1, 0.0) is False, "--flaky 0 never drops a request")
    check([should_flake(i, 1.0) for i in range(1, 4)] == [True] * 3,
          "--flaky 1 drops every request")
    check([should_flake(i, 0.5) for i in range(1, 7)]
          == [False, True, False, True, False, True],
          "--flaky 0.5 drops every second request, and the same one each run")
    check([should_flake(i, 0.34) for i in range(1, 7)]
          == [False, False, True, False, False, True],
          "--flaky 0.34 drops roughly every third, deterministically")
    check(len([i for i in range(1, 101) if should_flake(i, 0.2)]) == 20,
          "over a hundred requests --flaky 0.2 drops exactly twenty")
    check(should_flake(0, 1.0) is False,
          "an uncounted request is never dropped")
    raises(lambda: make_faults(flaky=1.5), "--flaky above 1 raises")
    raises(lambda: make_faults(flaky=-0.1), "--flaky below 0 raises")
    raises(lambda: make_faults(flaky=float("nan")),
           "a NaN --flaky raises, because it passes a range check written with "
           "< and > and then reaches int() as a crash  <-- pinned defect")
    check(make_faults(flaky=float("-0.0"))["flaky"] == 0.0,
          "negative zero is accepted and is just zero, so the NaN guard did "
          "not take a legal value with it")

    # ---- FAULT: the rest of the guards
    raises(lambda: make_faults(error_after=-1), "a negative --error-after raises")
    raises(lambda: make_faults(token_expires_after=-1),
           "a negative --token-expires-after raises")
    raises(lambda: make_faults(slow=-1), "a negative --slow raises")
    clean = make_faults()
    check(clean["error_after"] is None, "--error-after is off by default")
    check(clean["token_expires_after"] is None,
          "--token-expires-after is off by default")
    check(clean["truncate_page"] is False, "--truncate-page is off by default")
    check(clean["duplicate_oids"] is False, "--duplicate-oids is off by default")
    check(clean["drop_fields"] == [], "--drop-fields is empty by default")
    check(clean["slow"] == 0, "--slow is off by default")
    check(clean["flaky"] == 0.0, "--flaky is off by default")
    check(armed(build_spec(rows=1)) == [],
          "a spec with nothing armed reports no faults")
    check(armed(build_spec(rows=1, faults=make_faults(
        error_after=2, token_expires_after=3, truncate_page=True,
        duplicate_oids=True, drop_fields=["OWNER"], slow=250, flaky=0.25)))
          == ["--error-after 2", "--token-expires-after 3", "--truncate-page",
              "--duplicate-oids", "--drop-fields OWNER", "--slow 250",
              "--flaky 0.25"],
          "every armed fault is named back, so the banner tells the operator "
          "what this server is doing to them")

    # ---- routing
    check(route("/rest/info", {"f": "json"}, spec)["currentVersion"]
          == CURRENT_VERSION, "/rest/info routes")
    check(route("/arcgis/rest/info", {"f": "json"}, spec)["currentVersion"]
          == CURRENT_VERSION,
          "a web adaptor prefix routes the same, so a url copied off a real "
          "server works unchanged")
    check("services" in route("/rest/services", {"f": "json"}, spec),
          "/rest/services routes to the catalog")
    check(route("/rest/services/Parcels/FeatureServer", {"f": "json"},
                spec)["serviceDescription"].startswith("restfake"),
          "a service routes to its description")
    check(route("/rest/services/Parcels/FeatureServer/0", {"f": "json"},
                spec)["objectIdField"] == "OBJECTID",
          "a layer routes to its definition")
    check(len(route(path, {"where": "OBJECTID = 1", "f": "json"},
                    spec)["features"]) == 1, "a query routes to the query builder")
    check(route("/rest/services/Basemap/MapServer/0/query",
                {"where": "OBJECTID = 1", "f": "json"}, spec)["features"][0]
          ["attributes"]["OBJECTID"] == 1, "a MapServer layer answers a query too")
    body = route("/rest/services/Basemap/MapServer/0/addFeatures",
                 {"features": "[]", "f": "json"}, spec)
    check(is_error(body),
          "addFeatures against a MapServer is refused, because a MapServer "
          "cannot write")
    check(is_error(route("/rest/services/Nothing/FeatureServer", {"f": "json"},
                         spec)), "a service not in the catalog is an error")
    check(route("/rest/services/Nothing/FeatureServer",
                {"f": "json"}, spec)["error"]["code"] == 404,
          "and that error is a 404 code inside a 200 body  <-- pinned defect")
    check(is_error(route("/rest/services/Parcels/FeatureServer/7", {"f": "json"},
                         spec)), "a layer that does not exist is an error")
    check(is_error(route("/rest/services/Parcels/FeatureServer/x", {"f": "json"},
                         spec)), "a non-numeric layer id is an error")
    check(is_error(route("/rest/services/Parcels/FeatureServer/0/deleteFeatures",
                         {"f": "json"}, spec)),
          "an operation this fake does not implement is an error, not a crash")
    check(is_error(route("/rest/services/Parcels", {"f": "json"}, spec)),
          "a service path with no type is an error")
    check(is_error(route("/rest/nothing", {"f": "json"}, spec)),
          "an unknown path under /rest is an error")
    check(is_error(route("/favicon.ico", {"f": "json"}, spec)),
          "a path not under /rest at all is an error")
    check(route("/favicon.ico", {"f": "json"}, spec)["error"]["code"] == 404,
          "and it too is a 404 code inside a 200 body")
    check(rest_path("/arcgis/rest/services/A/FeatureServer/0")
          == ["services", "A", "FeatureServer", "0"],
          "the web adaptor prefix is stripped off the path")
    check(rest_path("/rest/") == [], "a bare /rest has no segments")
    check(rest_path("/nope/at/all") is None, "a path with no /rest is not routed")
    check(rest_path("/rest/services/A/FeatureServer/0/query?f=json")
          == ["services", "A", "FeatureServer", "0", "query"],
          "a query string is not part of the path")

    # ---- f=json, or the html page nobody expects
    check(isinstance(route("/rest/info", {}, spec), str),
          "a request with no f=json gets the html services directory page, not "
          "json  <-- pinned defect")
    check(isinstance(route("/rest/info", {"f": "html"}, spec), str),
          "f=html gets the html page too")
    check(isinstance(route("/rest/info", {"f": "pjson"}, spec), dict),
          "f=pjson is json")
    check(isinstance(route("/rest/info", {"f": "JSON"}, spec), dict),
          "f is matched case insensitively")
    check(isinstance(route(path, {"where": "1=1"}, spec), str),
          "a query with no f=json gets html as well, so a client that forgets "
          "it sees a doctype from json.loads")
    check(isinstance(route("/rest/generateToken",
                           {"username": "u", "password": "p"}, spec), str),
          "a sign-in without f=json gets the html page too, so a client that "
          "forgot it never sees the token it just asked for")
    check("<html>" in html_page("/rest/info"), "the html page is html")
    check("&lt;" in html_page("/rest/<script>"),
          "a path is escaped into the html page")

    # ---- the fault counter counts data requests only
    check(counts_toward_faults("/rest/services/Parcels/FeatureServer/0/query"),
          "a query counts toward the fault counters")
    check(counts_toward_faults(
        "/rest/services/Parcels/FeatureServer/0/addFeatures"),
          "an addFeatures counts")
    check(counts_toward_faults("/rest/info") is False,
          "/rest/info does not count  <-- pinned defect")
    check(counts_toward_faults("/rest/services") is False,
          "the catalog does not count")
    check(counts_toward_faults("/rest/services/Parcels/FeatureServer/0") is False,
          "a layer definition does not count, so --error-after means the same "
          "request whatever metadata a client read first")
    check(counts_toward_faults("/rest/generateToken") is False,
          "signing in does not count, or a token could expire before it was "
          "issued")
    check(counts_toward_faults("/favicon.ico") is False,
          "a path that is not routed does not count")

    # ---- the credential never reaches the log
    check(redact_query("token=%s&where=1%%3D1" % FAKE_TOKEN)
          == "token=[redacted]&where=1=1",
          "the token is taken out of a logged query string  <-- pinned defect")
    check(redact_query("username=gis_admin&password=hunter2")
          == "username=gis_admin&password=[redacted]",
          "the password is taken out of a logged post body  <-- pinned defect")
    for secret in sorted(SECRET_PARAMS):
        check("s3cret" not in redact_query("%s=s3cret" % secret),
              "the value of %s never reaches the log" % secret)
    check("s3cret" not in redact_query("TOKEN=s3cret"),
          "a parameter name in a different case is still redacted")
    check(redact_query("") == "", "an empty query logs as empty")
    check(redact_query("&&&") == "",
          "a query string that parses to no parameters at all logs as empty "
          "rather than as itself, because text nothing has read could be "
          "carrying anything")
    check(redact_query("where=1%3D1") == "where=1=1",
          "an ordinary parameter is logged intact, because a log with nothing "
          "in it is not a log")
    line = log_line("GET", path, "token=%s&f=json" % FAKE_TOKEN, 3, "200 ok")
    check(FAKE_TOKEN not in line, "no token reaches an access log line")
    check("#3" in line, "the log line carries the request number the faults count")
    check(log_line("GET", "/rest/info", "", 0, "200 ok").startswith("   GET"),
          "an uncounted request logs with no number")
    check("ERROR 500" in reply_note(error_envelope(500, "boom")),
          "an error envelope is logged as an ERROR, so the log does not read "
          "like a clean run")
    check(reply_note({"features": [1, 2], "exceededTransferLimit": True})
          == "200 2 feature(s) more", "a page logs its size and whether more follows")
    check(reply_note({"features": []}) == "200 0 feature(s)",
          "a last page logs without the more marker")
    check(reply_note({"addResults": [{"success": True}, {"success": False}]})
          == "200 1/2 added", "an addFeatures logs how many actually landed")
    check(reply_note({"token": "x"}) == "200 token issued",
          "a sign-in logs that a token was issued, not the token")
    check(reply_note(html_page("/")) == "200 text/html",
          "an html reply is logged as html")
    check(reply_note({"count": 5}) == "200 ok", "a count logs plainly")
    echo_probe = _FlushCounter()
    quiet, sys.stdout = sys.stdout, echo_probe
    try:
        _stdout_echo("#1 GET /rest/info 200 ok")
    finally:
        sys.stdout = quiet
    check(echo_probe.getvalue() == "#1 GET /rest/info 200 ok\n",
          "an access log line reaches stdout followed by a newline and nothing "
          "else, so the log is greppable a line at a time")
    check(echo_probe.flushes >= 1,
          "and every line is flushed as it is written, because redirected to a "
          "file python buffers 8KB first and a harness server killed at the "
          "end of a CI run loses the whole log  <-- pinned defect")

    # ---- the plan, printed when no socket is opened
    lines = plan_lines(spec, 7777)
    check(any("127.0.0.1:7777" in l for l in lines),
          "the plan names the address it would bind")
    check(any("loopback only" in l for l in lines),
          "and says that address is loopback only")
    check(any("3 page(s)" in l for l in lines),
          "the plan works out how many pages a full read takes")
    check(any("none armed" in l for l in lines),
          "a plan with no faults says so plainly")
    check(any("--error-after 2" in l for l in
              plan_lines(build_spec(rows=10, faults=make_faults(error_after=2)),
                         7777)),
          "and a plan with a fault names it")
    check(all("0.0.0.0" not in l for l in lines),
          "no line in the plan offers 0.0.0.0")
    check(page_plan(2500, 1000) == [(0, 1000), (1000, 1000), (2000, 500)],
          "the page plan for 2500 rows at 1000 is three pages")
    check(page_plan(2000, 1000) == [(0, 1000), (1000, 1000), (2000, 0)],
          "a row count that is a multiple of the page size needs a fourth, "
          "empty request to prove it ended  <-- pinned defect")
    check(page_plan(0, 1000) == [(0, 0)], "an empty layer is one request")
    check(page_plan(10, 1000) == [(0, 10)], "a small layer is one request")

    # ---- argument handling
    a = _parse([])
    check(a.apply is False, "--apply defaults to OFF, so no socket opens")
    check(a.port == 7777,
          "--port defaults to 7777, a fixed port a client can be pointed at "
          "without reading the banner first")
    check(a.rows == 2500,
          "--rows defaults to 2500, which is the row count that pages three "
          "times at the default page size and leaves the last page short")
    check(a.max_record_count == 1000,
          "--max-record-count defaults to 1000, the cap a hosted feature "
          "service ships with, so the default fixture pages like production")
    check(a.error_after is None, "--error-after defaults to OFF")
    check(a.token_expires_after is None, "--token-expires-after defaults to OFF")
    check(a.truncate_page is False, "--truncate-page defaults to OFF")
    check(a.duplicate_oids is False, "--duplicate-oids defaults to OFF")
    check(a.drop_fields == "", "--drop-fields defaults to empty")
    check(a.slow == 0, "--slow defaults to OFF")
    check(a.flaky == 0.0, "--flaky defaults to OFF")
    check(a.self_test is False, "--self-test defaults to OFF")
    check(_parse(["--apply"]).apply is True, "--apply is read")
    check(_parse(["--port", "9001"]).port == 9001, "--port is read")
    check(_parse(["--rows", "42"]).rows == 42, "--rows is read")
    check(_parse(["--max-record-count", "7"]).max_record_count == 7,
          "--max-record-count is read")
    check(_parse(["--error-after", "3"]).error_after == 3,
          "--error-after is read")
    check(_parse(["--token-expires-after", "4"]).token_expires_after == 4,
          "--token-expires-after is read")
    check(_parse(["--truncate-page"]).truncate_page is True,
          "--truncate-page is read")
    check(_parse(["--duplicate-oids"]).duplicate_oids is True,
          "--duplicate-oids is read")
    check(_parse(["--drop-fields", "OWNER,ACRES"]).drop_fields == "OWNER,ACRES",
          "--drop-fields is read")
    check(_parse(["--slow", "250"]).slow == 250, "--slow is read")
    check(_parse(["--flaky", "0.25"]).flaky == 0.25, "--flaky is read")
    check(_parse(["--self-test"]).self_test is True, "--self-test is read")
    refuses(["--host", "0.0.0.0"],
            "there is no --host flag, so nothing on the command line can move "
            "the bind off loopback  <-- pinned defect")
    refuses(["--bind", "0.0.0.0"], "and no --bind flag either")
    refuses(["--port", "nope"], "a non-numeric --port is refused")
    refuses(["--flaky", "nope"], "a non-numeric --flaky is refused")
    check(faults_from_args(_parse(["--drop-fields", "OWNER, ACRES"]))
          ["drop_fields"] == ["OWNER", "ACRES"],
          "--drop-fields splits on commas and ignores the spaces around them")
    check(faults_from_args(_parse([]))["drop_fields"] == [],
          "no --drop-fields drops nothing")
    check(faults_from_args(_parse(["--flaky", "0.5"]))["flaky"] == 0.5,
          "the fault set is built from the parsed arguments")
    check(exits(["--rows", "0", "--max-record-count", "0"]) == 64,
          "a maxRecordCount of zero is a usage error, not a traceback")
    check(exits(["--flaky", "2"]) == 64, "--flaky 2 is a usage error")
    check(exits(["--drop-fields", "NOPE"]) == 64,
          "--drop-fields naming an unknown field is a usage error")
    check(exits(["--port", "0"]) == 64,
          "--port 0 is refused on the command line, because a port the "
          "operator cannot predict is no use to a client")
    check(exits(["--port", "70000"]) == 64, "a port above 65535 is a usage error")
    check(_parse(["--flaky", "nan"]).flaky != _parse(["--flaky", "nan"]).flaky,
          "argparse reads the word nan as a float and hands back a NaN, so "
          "--flaky cannot be range checked by its type alone  <-- pinned defect")
    check(exits(["--flaky", "nan"]) == 64,
          "and --flaky nan is refused as a usage error, because NaN answers "
          "False to every < and > and would otherwise reach should_flake and "
          "kill the first data request  <-- pinned defect")
    check(exits(["--flaky", "-0.0"]) == 0,
          "negative zero is still zero and is accepted, because a refusal a "
          "client cannot explain is worse than the fault it prevents")
    result, printed = captured(lambda: main(["--rows", "10"]))
    check(result == 0, "a run with no --apply exits 0")
    check("No socket was opened" in printed,
          "and says plainly that nothing was bound  <-- pinned defect")
    check("127.0.0.1" in printed, "the plan it printed names the loopback bind")
    check("0.0.0.0" not in printed, "and never names 0.0.0.0")

    # ---- the harness itself, which has to be able to report red
    #
    # A self-test whose failure path is never exercised is not a control: it
    # reports green because nothing ever runs the other branch. It is driven
    # here against four deliberate failures, with its output swallowed and its
    # tally put back, so a green run has still proven it can go red.
    def probe():
        check(False, "a false check must be recorded as a failure")
        raises(lambda: None, "a call that raises nothing must fail")
        raises(lambda: 1 / 0, "a call that raises the wrong thing must fail")
        refuses(["--self-test"], "an argv argparse accepts must fail")

    kept_passed, kept_failed = passed[0], list(failed)
    _ignored, noise = captured(probe)
    probe_passed, probe_failed = passed[0], list(failed)
    passed[0], failed[:] = kept_passed, kept_failed
    check(len(probe_failed) - len(kept_failed) == 4,
          "the harness records a false check, a missing exception, a wrong "
          "exception and an argv argparse accepted as four failures, so a "
          "broken tool turns this self-test red  <-- pinned defect")
    check(probe_passed == kept_passed, "not one of those four counted as a pass")
    check(noise.count("FAIL  ") == 4,
          "every recorded failure prints a FAIL line an operator can see")

    # ---- the socket layer, over a real loopback connection
    #
    # Everything above is a pure function. This section opens a real socket on
    # 127.0.0.1, drives it with urllib, and proves the shell wires the builders
    # up: that a fault really arrives as HTTP 200 with an error body, that a
    # dropped connection really drops, and that no credential reaches the log.
    log = []
    live = build_spec(rows=250, max_record_count=100,
                      faults=make_faults(error_after=2, slow=40))
    server = build_server(live, 0, log.append)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        base = "http://%s:%d" % (BIND_HOST, port)

        def fetch(route_path, **params):
            url = "%s%s?%s" % (base, route_path, urllib.parse.urlencode(params))
            with urllib.request.urlopen(url, timeout=15) as response:
                return (response.status, response.headers.get("Content-Type"),
                        response.read().decode("utf-8"))

        check(server.server_address[0] == BIND_HOST,
              "the running server is bound to 127.0.0.1 and nothing else")
        check(server.socket.getsockname()[0] == "127.0.0.1",
              "the listening socket agrees  <-- pinned defect")
        check(server.socket.family == socket.AF_INET, "over ipv4")

        started = time.time()
        status, ctype, text = fetch("/rest/info", f="json")
        check(time.time() - started >= 0.04,
              "--slow 40 delays a metadata request too, not only the data "
              "requests the fault counter counts, so a client's connect "
              "timeout sees it on the very first call")
        check(status == 200, "/rest/info answers 200 over the wire")
        check("application/json" in ctype, "with a json content type")
        check(json.loads(text)["currentVersion"] == CURRENT_VERSION,
              "and a version a client can gate on")

        status, _ctype, text = fetch("/rest/services", f="json")
        check([s["name"] for s in json.loads(text)["services"]]
              == ["Parcels", "Basemap"], "the catalog answers over the wire")

        status, _ctype, text = fetch(
            "/rest/services/Parcels/FeatureServer/0", f="json")
        check(len(json.loads(text)["fields"]) == len(FIELDS),
              "the layer definition answers over the wire with all six fields")

        live_query = "/rest/services/Parcels/FeatureServer/0/query"
        started = time.time()
        status, _ctype, text = fetch(live_query, where="1=1",
                                     resultRecordCount="100", f="json")
        elapsed = time.time() - started
        page1 = json.loads(text)
        check(status == 200, "the first page answers 200")
        check(len(page1["features"]) == 100, "with a full page of features")
        check(page1["exceededTransferLimit"] is True, "and says there is more")
        check(elapsed >= 0.04,
              "--slow 40 really delayed the response by at least 40ms")

        status, _ctype, text = fetch(live_query, where="1=1", resultOffset="100",
                                     resultRecordCount="100", f="json")
        page2 = json.loads(text)
        ids = ([f["attributes"]["OBJECTID"] for f in page1["features"]]
               + [f["attributes"]["OBJECTID"] for f in page2["features"]])
        check(len(set(ids)) == 200,
              "two pages over the wire carry 200 distinct oids")

        # The headline, proven over a socket: request three is the one
        # --error-after 2 breaks, and it arrives as a 200.
        status, ctype, text = fetch(live_query, where="1=1", f="json")
        body = json.loads(text)
        check(status == 200,
              "the FAILING request answers HTTP 200  <-- pinned defect")
        check(200 <= status < 300,
              "a client checking only the status code sees success  "
              "<-- pinned defect")
        check(is_error(body), "while the body is an error envelope")
        check(body["error"]["code"] == 500, "carrying arcgis code 500")
        check("features" not in body,
              "and no features key, so the client that ignored the body fails "
              "on the next line instead of shipping an empty layer")
        check("application/json" in ctype,
              "the failure is even served with a json content type")

        status, ctype, text = fetch("/rest/info")
        check("text/html" in ctype,
              "a request without f=json really is served html over the wire  "
              "<-- pinned defect")
        check(text.startswith("<html>"), "and the body really is a page")

        status, _ctype, text = fetch("/rest/nothing", f="json")
        check(status == 200 and is_error(json.loads(text)),
              "an unknown path is a 200 with a 404 inside it, over the wire")

        token_url = "%s/rest/generateToken" % base
        form = urllib.parse.urlencode({"username": "gis_admin",
                                       "password": "hunter2",
                                       "f": "json"}).encode("utf-8")
        with urllib.request.urlopen(token_url, data=form, timeout=15) as response:
            issued = json.loads(response.read().decode("utf-8"))
        check(issued["token"] == FAKE_TOKEN, "a POST sign-in returns a token")

        add_url = "%s/rest/services/Parcels/FeatureServer/0/addFeatures" % base
        form = urllib.parse.urlencode({
            "features": json.dumps([{"attributes": {"OWNER": "NEW"}}]),
            "f": "json"}).encode("utf-8")
        with urllib.request.urlopen(add_url, data=form, timeout=15) as response:
            added = json.loads(response.read().decode("utf-8"))
        check(status == 200 and "addResults" in added,
              "a POST addFeatures answers over the wire")
        check(added["addResults"][0]["success"] is False,
              "and by now --error-after 2 has it failing per feature, inside a "
              "200, with no top-level error  <-- pinned defect")
        check(is_error(added) is False,
              "which a client checking for a top-level error calls a clean write")

        check("hunter2" not in "\n".join(log),
              "the password never reached the access log  <-- pinned defect")
        check(FAKE_TOKEN not in "\n".join(log),
              "and neither did the token  <-- pinned defect")
        check(any(REDACTED in l for l in log),
              "the log shows the redaction, so it is visibly doing it")
        check(any("gis_admin" in l for l in log),
              "the username is kept, because a log that hides everything is "
              "useless")
        check(any("ERROR 500" in l for l in log),
              "the log records the failing request as an error")
        check(len(log) >= 9, "every request reached the log")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)

    # ---- the socket layer again, for the two faults only it can produce
    log2 = []
    live2 = build_spec(rows=50, max_record_count=10,
                       faults=make_faults(token_expires_after=3, flaky=0.5))
    server2 = build_server(live2, 0, log2.append)
    port2 = server2.server_address[1]
    thread2 = threading.Thread(target=server2.serve_forever)
    thread2.daemon = True
    thread2.start()
    # BaseHTTPRequestHandler's own logger writes self.path to stderr, token and
    # all, and this server overrides it to silence. An override nothing checks
    # is not a control, so stderr is collected across the whole exchange below,
    # which is the one section that puts a token on the wire.
    handler_noise = io.StringIO()
    real_stderr, sys.stderr = sys.stderr, handler_noise
    try:
        base2 = "http://%s:%d" % (BIND_HOST, port2)
        query2 = "%s/rest/services/Parcels/FeatureServer/0/query" % base2

        def query(**params):
            url = "%s?%s" % (query2, urllib.parse.urlencode(params))
            with urllib.request.urlopen(url, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))

        # --flaky 0.5 drops requests 2, 4, 6. Requests 1, 3 and 5 answer.
        status, body = query(where="1=1", token=FAKE_TOKEN, f="json")
        check(status == 200 and len(body["features"]) == 10,
              "request 1 answers with a token")
        dropped_exc = None
        try:
            query(where="1=1", token=FAKE_TOKEN, f="json")
        except Exception as exc:
            dropped_exc = exc
        check(dropped_exc is not None,
              "request 2 is dropped at the transport level by --flaky 0.5, "
              "which no error envelope can express  <-- pinned defect")
        check(isinstance(dropped_exc, (urllib.error.URLError,
                                       http.client.HTTPException, OSError)),
              "and it reaches the client as a connection error, not a response")
        status, body = query(where="1=1", token=FAKE_TOKEN, f="json")
        check(status == 200,
              "request 3 answers again, because --flaky is deterministic and "
              "not a coin toss")
        check(is_error(body) is False,
              "and the token is still good on request 3 of its three")
        try:
            query(where="1=1", token=FAKE_TOKEN, f="json")
        except Exception:
            pass                                  # request 4, dropped
        status, body = query(where="1=1", token=FAKE_TOKEN, f="json")
        check(status == 200,
              "request 5 answers 200, in the middle of a paging loop")
        check(is_error(body),
              "and the token that worked a moment ago is now refused  "
              "<-- pinned defect")
        check(body["error"]["code"] == 498,
              "with arcgis code 498, inside that 200")

        anon_url = "%s?%s" % (query2, urllib.parse.urlencode(
            {"where": "1=1", "f": "json"}))
        anon = None
        for _attempt in range(2):
            try:
                with urllib.request.urlopen(anon_url, timeout=15) as response:
                    anon = json.loads(response.read().decode("utf-8"))
                break
            except Exception:
                continue                          # dropped, try the next one
        check(anon is not None,
              "one of two anonymous attempts got past --flaky 0.5")
        check(anon["error"]["code"] == 499,
              "an anonymous request is refused once token security is armed")
        check(any("#2" in l and "DROPPED" in l for l in log2),
              "a dropped request still took a request number, so retrying "
              "after a transport failure does not win its fault budget back  "
              "<-- pinned defect")
        check(FAKE_TOKEN not in "\n".join(log2),
              "the token never reached this server's log either")
        check(any("DROPPED" in l for l in log2),
              "the log records the dropped connections, which is the only "
              "trace of them anywhere")
    finally:
        server2.shutdown()
        server2.server_close()
        thread2.join(10)
        sys.stderr = real_stderr

    check(FAKE_TOKEN not in handler_noise.getvalue(),
          "no token reached stderr either, which the access log's redaction "
          "would never have caught  <-- pinned defect")
    check(handler_noise.getvalue() == "",
          "and nothing at all did: the handler's default logger, which prints "
          "the whole url including the token, is really overridden  "
          "<-- pinned defect")

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="restfake.py",
        description="A mock ArcGIS REST server that fails the way ArcGIS "
                    "actually fails: 200 OK with an error body, a token that "
                    "expires mid-paging, a page that comes back short.",
        epilog="Binds %s only. No socket is opened without --apply."
               % BIND_HOST,
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="loopback port to serve on (default %d)" % DEFAULT_PORT)
    ap.add_argument("--rows", type=int, default=DEFAULT_ROWS,
                    help="rows in the fake layer (default %d)" % DEFAULT_ROWS)
    ap.add_argument("--max-record-count", dest="max_record_count", type=int,
                    default=DEFAULT_MAX_RECORD_COUNT,
                    help="rows the layer will return in one page "
                         "(default %d)" % DEFAULT_MAX_RECORD_COUNT)
    ap.add_argument("--error-after", dest="error_after", type=int, default=None,
                    help="answer the first N data requests normally, then "
                         "return HTTP 200 with an error envelope")
    ap.add_argument("--token-expires-after", dest="token_expires_after",
                    type=int, default=None,
                    help="require a token, and stop accepting the issued one "
                         "after N data requests")
    ap.add_argument("--truncate-page", dest="truncate_page", action="store_true",
                    help="return half the features a page promised, while "
                         "still reporting exceededTransferLimit")
    ap.add_argument("--duplicate-oids", dest="duplicate_oids",
                    action="store_true",
                    help="start each page before the previous one ended, so "
                         "OBJECTIDs repeat across pages")
    ap.add_argument("--drop-fields", dest="drop_fields", default="",
                    help="comma separated fields to leave out of query "
                         "responses while the layer definition still "
                         "advertises them")
    ap.add_argument("--slow", type=int, default=0,
                    help="delay every response by this many milliseconds")
    ap.add_argument("--flaky", type=float, default=0.0,
                    help="fraction of data requests to drop at the transport "
                         "level, deterministically (0 to 1)")
    ap.add_argument("--apply", action="store_true",
                    help="open the socket and serve. Without it the plan is "
                         "printed and no port is bound.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the assertions and exit")
    return ap.parse_args(argv)


def faults_from_args(args):
    return make_faults(
        error_after=args.error_after,
        token_expires_after=args.token_expires_after,
        truncate_page=args.truncate_page,
        duplicate_oids=args.duplicate_oids,
        drop_fields=args.drop_fields.split(","),
        slow=args.slow,
        flaky=args.flaky,
    )


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if args.port < 1 or args.port > 65535:
        print("error: --port must be between 1 and 65535, got %d. A client has "
              "to be able to predict the port." % args.port, file=sys.stderr)
        return 64
    try:
        spec = build_spec(args.rows, args.max_record_count, faults_from_args(args))
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64

    print("restfake: a mock ArcGIS REST server that fails the way ArcGIS "
          "actually fails")
    for line in plan_lines(spec, args.port):
        print(line)

    if not args.apply:
        print("")
        print("Check only. No socket was opened. Re-run with --apply to serve.")
        return 0

    try:
        server = build_server(spec, args.port, _stdout_echo)
    except OSError as exc:
        print("error: could not bind %s:%d: %s"
              % (BIND_HOST, args.port, exc), file=sys.stderr)
        return 2

    print("")
    print("serving. Ctrl-C to stop.")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("")
        print("stopped after %d data request(s)." % server.requests)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
