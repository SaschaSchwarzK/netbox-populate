#!/usr/bin/env python3
"""Populate a NetBox instance with extra demo data, driven by YAML files.

The engine is generic: every file in ``data/`` describes ONE API endpoint and a
list of objects to create there. Files are processed in filename order, so a
numeric prefix defines the dependency order (``010-...`` before ``020-...``).

File format
-----------
    endpoint: dcim/sites          # required: "<app>/<endpoint>" as in the REST API path
    lookup: [slug]                # fields identifying an existing object (default: [name])
    mode: create                  # create (default) | update | upsert
    defaults: {status: active}    # merged into every object of the file
    plain_tags: false             # true only where "tags" is a plain list of slugs (config contexts)
    lookup_mode: filter           # "scan" = list everything and compare locally; for endpoints that
                                  # have no filter set (extras/subscriptions, extras/notifications)
    objects:
      - name: Hogwarts Castle
        slug: hogwarts-castle
        region: "@dcim/regions?slug=middle-earth"     # reference, see below

Modes
    create  create the object unless one matching ``lookup`` already exists
    update  PATCH an existing object found via ``lookup`` (error if missing);
            used for relations that need both objects to exist first, such as
            ``primary_ip4``, a virtual chassis ``master`` or a device bay's ``installed_device``
    upsert  create when missing, otherwise PATCH
    --cleanup deletes created objects in reverse order; ``update`` files are reverted
            (their fields are emptied) so that dependants can be removed.

References
    Any string value that starts with ``@`` is a reference to another object,
    written as ``@<app>/<endpoint>?<filter>=<value>&<filter>=<value>``. It is
    resolved with a GET request and replaced by the object's numeric id, so it
    works for foreign keys, lists of ids and generic ``object_id`` fields.
    The filter must match exactly one object. Endpoints without a filter set
    (extras/notification-groups, extras/subscriptions, extras/notifications) need the
    mapping form with ``$scan: true`` (see below). Use ``%26`` for a literal ``&``
    and ``@@`` to write a literal leading ``@``.

Lookup filter names
    A reference can also be written as a mapping, which allows nested references:
    ``{$ref: dcim/power-port-templates, device_type_id: "@dcim/device-types?slug=x", name: PSU1}``.

    ``lookup`` entries are field names. When the field's value is a reference,
    the filter ``<field>_id`` is used automatically (``device`` -> ``device_id``).
    Use the dict form to override: ``lookup: {assigned_object_id: assigned_object_id}``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

import pynetbox
import yaml
from pynetbox.core.query import RequestError

MODES = ("create", "update", "upsert")
REF_RE = re.compile(r"^@(?P<endpoint>[a-z0-9-]+/[a-z0-9-]+)\?(?P<query>.*)$")


class DataError(Exception):
    """Problem with a data file or a reference (not an API failure)."""


class RefNotFound(DataError):
    """A reference could not be resolved (the referenced object does not exist)."""


class Stats:
    def __init__(self) -> None:
        self.created = self.existing = self.updated = self.deleted = self.failed = self.skipped = 0

    def add(self, other: "Stats") -> None:
        for name in ("created", "existing", "updated", "deleted", "failed", "skipped"):
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def line(self, cleanup: bool) -> str:
        if cleanup:
            return f"deleted {self.deleted}, reset {self.updated}, not found {self.existing}, failed {self.failed}"
        return (
            f"created {self.created}, existing {self.existing}, updated {self.updated}, "
            f"skipped {self.skipped}, failed {self.failed}"
        )


class Populator:
    def __init__(self, nb: pynetbox.api, dry_run: bool, verbose: bool, fail_fast: bool) -> None:
        self.nb = nb
        self.dry_run = dry_run
        self.verbose = verbose
        self.fail_fast = fail_fast
        self.ref_cache: dict[tuple, int] = {}
        self.unresolved_in_dry_run = 0

    # ------------------------------------------------------------------ helpers
    def endpoint(self, path: str):
        try:
            app, name = path.strip("/").split("/", 1)
            return getattr(getattr(self.nb, app), name.replace("-", "_"))
        except (ValueError, AttributeError) as exc:
            raise DataError(f"invalid endpoint '{path}'") from exc

    def endpoint_available(self, path: str) -> bool:
        try:
            self.endpoint(path).count()
            return True
        except RequestError as exc:
            if getattr(exc.req, "status_code", None) == 404:
                return False
            raise

    @staticmethod
    def parse_query(query: str) -> dict[str, str]:
        params: dict[str, str] = {}
        for part in query.split("&"):
            if not part:
                continue
            key, _, value = part.partition("=")
            params[unquote(key)] = unquote(value)
        if not params:
            raise DataError("reference has no filter")
        return params

    def lookup_ref(self, ref: str) -> int:
        match = REF_RE.match(ref)
        if not match:
            raise DataError(f"malformed reference '{ref}' (expected @app/endpoint?field=value)")
        return self.lookup_params_ref(match["endpoint"], self.parse_query(match["query"]), ref)

    def lookup_params_ref(self, endpoint_path: str, params: dict, shown: str) -> int:
        key = (endpoint_path, tuple(sorted((k, str(v)) for k, v in params.items())))
        if key in self.ref_cache:
            return self.ref_cache[key]
        found = list(self.endpoint(endpoint_path).filter(**params))
        if len(found) == 1:
            self.ref_cache[key] = found[0].id
            return found[0].id
        if not found:
            if self.dry_run:
                self.unresolved_in_dry_run += 1
                return 0
            raise RefNotFound(f"reference not found: {shown}")
        raise DataError(
            f"reference is ambiguous ({len(found)} matches): {shown} "
            "(if the endpoint has no filter for this field NetBox ignores it - use another filter such as q=)"
        )

    def scan_ref(self, endpoint_path: str, params: dict) -> int:
        key = (endpoint_path, "scan", tuple(sorted((k, str(v)) for k, v in params.items())))
        if key in self.ref_cache:
            return self.ref_cache[key]
        found = [
            rec for rec in self.endpoint(endpoint_path).all()
            if all(str(self.field_value(rec, k)).lower() == str(v).lower() for k, v in params.items())
        ]
        if len(found) == 1:
            self.ref_cache[key] = found[0].id
            return found[0].id
        if not found:
            if self.dry_run:
                self.unresolved_in_dry_run += 1
                return 0
            raise RefNotFound(f"reference not found: {endpoint_path} {params}")
        raise DataError(f"reference is ambiguous ({len(found)} matches): {endpoint_path} {params}")

    @staticmethod
    def is_ref(value) -> bool:
        if isinstance(value, str):
            return value.startswith("@") and not value.startswith("@@")
        return isinstance(value, dict) and "$ref" in value

    def resolve(self, value):
        if isinstance(value, str):
            if value.startswith("@@"):
                return value[1:]
            if value.startswith("@"):
                return self.lookup_ref(value)
            return value
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        if isinstance(value, dict) and "$ref" in value:
            # Nested reference: {$ref: app/endpoint, <filter>: <value or @reference>, ...}
            # Needed when the target has no globally unique name (e.g. component templates).
            # Add "$scan: true" for endpoints without a filter set: all objects are listed and
            # compared locally instead of being filtered by NetBox.
            params = {k: self.resolve(v) for k, v in value.items() if k not in ("$ref", "$scan")}
            if value.get("$scan"):
                return self.scan_ref(value["$ref"], params)
            return self.lookup_params_ref(value["$ref"], params, f"{value['$ref']} {params}")
        if isinstance(value, dict):
            return {key: self.resolve(item) for key, item in value.items()}
        if isinstance(value, dt.datetime):
            return value.isoformat()
        if isinstance(value, dt.date):
            return value.isoformat()
        return value

    @staticmethod
    def normalize_tags(payload: dict, plain: bool) -> None:
        """Turn ``tags: [slug, ...]`` into nested tag references.

        Endpoints that take a plain list of slugs (config contexts) set ``plain_tags: true``.
        """
        tags = payload.get("tags")
        if plain or not isinstance(tags, list):
            return
        payload["tags"] = [{"slug": t} if isinstance(t, str) else t for t in tags]

    def lookup_params(self, spec: dict, raw: dict, payload: dict) -> dict:
        lookup = spec.get("lookup", ["name"])
        mapping = lookup if isinstance(lookup, dict) else {name: None for name in lookup}
        params: dict = {}
        for field, override in mapping.items():
            if field not in payload:
                continue
            value = payload[field]
            if isinstance(value, (list, dict)) or value is None:
                continue
            if override:
                params[override] = value
            elif self.is_ref(raw.get(field)):
                params[field if field.endswith("_id") else f"{field}_id"] = value
            else:
                params[field] = value
        if not params:
            raise DataError(f"none of the lookup fields {list(mapping)} is set on the object")
        return params

    @staticmethod
    def field_value(record, field):
        actual = getattr(record, field, None)
        if actual is None:
            return None
        return getattr(actual, "id", None) if hasattr(actual, "id") else getattr(actual, "value", actual)

    def scan_existing(self, ep, spec: dict, payload: dict):
        """Client-side lookup for endpoints without a filter set (subscriptions, notifications)."""
        lookup = spec.get("lookup", ["name"])
        fields = [f for f in (lookup if isinstance(lookup, list) else list(lookup))
                  if f in payload and not isinstance(payload[f], (list, dict)) and payload[f] is not None]
        if not fields:
            raise DataError(f"none of the lookup fields {lookup} is set on the object")
        for record in ep.all():
            if all(str(self.field_value(record, f)).lower() == str(payload[f]).lower() for f in fields):
                return record
        return None

    def find_existing(self, ep, params: dict, spec: dict, payload: dict):
        if spec.get("lookup_mode") == "scan":
            return self.scan_existing(ep, spec, payload)
        found = list(ep.filter(**params))
        if len(found) > 1:
            raise DataError(f"lookup {params} matches {len(found)} objects; make it more specific")
        if found:
            self.verify_match(found[0], spec, payload, params)
        return found[0] if found else None

    @staticmethod
    def verify_match(record, spec: dict, payload: dict, params: dict) -> None:
        """NetBox silently ignores unknown filter names, which would return an unrelated object.

        Compare the lookup fields of the found record with the wanted values to catch that.
        """
        lookup = spec.get("lookup", ["name"])
        for field in (lookup if isinstance(lookup, list) else list(lookup)):
            if field not in payload or isinstance(payload[field], (list, dict)) or payload[field] is None:
                continue
            actual = getattr(record, field, None)
            if actual is None:
                continue
            actual = getattr(actual, "id", None) if hasattr(actual, "id") else getattr(actual, "value", actual)
            if str(actual).lower() != str(payload[field]).lower():
                raise DataError(
                    f"lookup filter {params} returned an object whose '{field}' is '{actual}', not "
                    f"'{payload[field]}' - check the filter name (NetBox ignores unknown filters)"
                )

    @staticmethod
    def label(spec: dict, raw: dict) -> str:
        for key in ("name", "label", "slug", "model", "address", "prefix", "vid", "mac_address", "cid", "term_side"):
            if raw.get(key) not in (None, ""):
                return f"{spec['endpoint']} '{raw[key]}'"
        return f"{spec['endpoint']} {str(raw)[:60]}"

    @staticmethod
    def api_error(exc: Exception) -> str:
        if isinstance(exc, RequestError):
            return str(getattr(exc, "error", exc))
        return str(exc)

    # ---------------------------------------------------------------- processing
    def process_file(self, path: Path, cleanup: bool) -> Stats:
        stats = Stats()
        try:
            spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise DataError(f"{path.name}: invalid YAML: {exc}") from exc
        if not isinstance(spec, dict):
            raise DataError(f"{path.name}: top level must be a mapping")
        if "endpoint" not in spec:
            raise DataError(f"{path.name}: missing 'endpoint'")
        mode = spec.get("mode", "create")
        if mode not in MODES:
            raise DataError(f"{path.name}: unknown mode '{mode}'")
        objects = spec.get("objects") or []
        if not self.endpoint_available(spec["endpoint"]):
            print(f"  SKIP  endpoint {spec['endpoint']} is not available on this NetBox version")
            stats.skipped = len(objects)
            return stats
        ep = self.endpoint(spec["endpoint"])
        defaults = spec.get("defaults") or {}
        sequence = reversed(objects) if cleanup else objects
        for raw_obj in sequence:
            raw = {**defaults, **raw_obj}
            raw = {k: v for k, v in raw.items() if not str(k).startswith("_")}
            name = self.label(spec, raw)
            try:
                if cleanup:
                    # Only the fields that identify the object are resolved: other references may
                    # already be gone, which must not hide an object that still exists.
                    lookup = spec.get("lookup", ["name"])
                    keys = lookup if isinstance(lookup, list) else list(lookup)
                    payload = self.resolve({k: v for k, v in raw.items() if k in keys})
                else:
                    payload = self.resolve(raw)
                self.normalize_tags(payload, bool(spec.get("plain_tags")))
                params = {} if spec.get("lookup_mode") == "scan" else self.lookup_params(spec, raw, payload)
                existing = self.find_existing(ep, params, spec, payload)
                if cleanup and mode == "update":
                    self.revert_one(spec, name, raw, existing, stats)
                elif cleanup:
                    self.cleanup_one(name, existing, stats)
                else:
                    self.apply_one(ep, mode, name, payload, existing, stats)
            except RefNotFound:
                if not cleanup:
                    stats.failed += 1
                    print(f"  FAIL  {name}: {self.api_error(sys.exc_info()[1])[:600]}")
                    if self.fail_fast:
                        raise SystemExit(1)
                else:
                    stats.existing += 1  # a referenced object is gone, so this one cannot exist either
            except (DataError, RequestError) as exc:
                if cleanup and isinstance(exc, RequestError) and getattr(exc.req, "status_code", None) == 400:
                    stats.existing += 1  # lookup used a filter value that no longer exists
                    continue
                stats.failed += 1
                print(f"  FAIL  {name}: {self.api_error(exc)[:600]}")
                if self.fail_fast:
                    raise SystemExit(1)
        return stats

    def apply_one(self, ep, mode: str, name: str, payload: dict, existing, stats: Stats) -> None:
        if existing is None:
            if mode == "update":
                raise DataError("object to update does not exist")
            if self.dry_run:
                print(f"  would create  {name}")
            else:
                ep.create(payload)
                self.say(f"  created  {name}")
            stats.created += 1
        elif mode == "create":
            stats.existing += 1
            self.say(f"  exists   {name}")
        else:
            if self.dry_run:
                print(f"  would update  {name}")
            else:
                if not existing.update(payload):
                    self.say(f"  unchanged  {name}")
                    stats.existing += 1
                    return
                self.say(f"  updated  {name}")
            stats.updated += 1

    def revert_one(self, spec: dict, name: str, raw: dict, existing, stats: Stats) -> None:
        """Cleanup of an ``update`` file: empty the fields it set so that dependants can be deleted."""
        if existing is None:
            stats.existing += 1
            return
        lookup = spec.get("lookup", ["name"])
        keep = set(lookup if isinstance(lookup, list) else lookup.keys())
        reset = {k: ([] if isinstance(v, list) else None) for k, v in raw.items() if k not in keep}
        if self.dry_run:
            print(f"  would reset  {name}: {sorted(reset)}")
        else:
            existing.update(reset)
            self.say(f"  reset  {name}")
        stats.updated += 1

    def cleanup_one(self, name: str, existing, stats: Stats) -> None:
        if existing is None:
            stats.existing += 1
            return
        if self.dry_run:
            print(f"  would delete  {name}")
        else:
            existing.delete()
            self.say(f"  deleted  {name}")
        stats.deleted += 1

    def say(self, message: str) -> None:
        if self.verbose:
            print(message)


# ----------------------------------------------------------------- offline check
def iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)


def validate_files(files: list[Path]) -> int:
    """Check structure and reference syntax of the data files without contacting NetBox."""
    problems = 0
    for path in files:
        def bad(message: str) -> None:
            nonlocal problems
            problems += 1
            print(f"  {path.name}: {message}")
        try:
            spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            bad(f"invalid YAML: {exc}")
            continue
        if not isinstance(spec, dict) or "endpoint" not in spec:
            bad("top level must be a mapping with 'endpoint'")
            continue
        if not re.fullmatch(r"[a-z0-9-]+/[a-z0-9-]+", str(spec["endpoint"])):
            bad(f"endpoint '{spec['endpoint']}' must look like app/endpoint")
        if spec.get("mode", "create") not in MODES:
            bad(f"unknown mode '{spec.get('mode')}'")
        if spec.get("lookup_mode", "filter") not in ("filter", "scan"):
            bad(f"unknown lookup_mode '{spec.get('lookup_mode')}'")
        objects = spec.get("objects")
        if not isinstance(objects, list) or not objects:
            bad("'objects' must be a non-empty list")
            continue
        lookup = spec.get("lookup", ["name"])
        keys = lookup if isinstance(lookup, list) else list(lookup)
        seen: set = set()
        for index, obj in enumerate(objects, 1):
            if not isinstance(obj, dict):
                bad(f"object #{index} is not a mapping")
                continue
            merged = {**(spec.get("defaults") or {}), **obj}
            # Lookup fields may be optional (e.g. vrf); at least one must identify the object.
            if not any(k in merged for k in keys) and spec.get("lookup_mode") != "scan":
                bad(f"object #{index} has none of the lookup fields {keys}")
            identity = tuple(str(merged.get(k)) for k in keys)
            if identity in seen:
                bad(f"object #{index} duplicates lookup {identity}")
            seen.add(identity)
            for text in iter_strings(merged):
                if text.startswith("@") and not text.startswith("@@") and not REF_RE.match(text):
                    bad(f"object #{index}: malformed reference '{text}'")
            for value in merged.values():
                for sub in (value if isinstance(value, list) else [value]):
                    if isinstance(sub, dict) and "$ref" in sub and not re.fullmatch(r"[a-z0-9-]+/[a-z0-9-]+", str(sub["$ref"])):
                        bad(f"object #{index}: malformed $ref '{sub['$ref']}'")
    print(f"validated {len(files)} files, {problems} problem(s)")
    return 1 if problems else 0


# ---------------------------------------------------------------------- CLI
def select_files(data_dir: Path, only: str | None, skip: str | None) -> list[Path]:
    files = sorted(p for p in data_dir.glob("*.yml") if p.is_file())
    if only:
        files = [p for p in files if re.search(only, p.name)]
    if skip:
        files = [p for p in files if not re.search(skip, p.name)]
    return files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Populate NetBox with extra demo data.")
    parser.add_argument("--url", default=os.environ.get("NETBOX_URL"), help="NetBox base URL (env NETBOX_URL)")
    parser.add_argument("--token", default=os.environ.get("NETBOX_TOKEN"),
                        help="API token, v1 or v2 'nbt_...' (prefer env NETBOX_TOKEN; never printed)")
    parser.add_argument("--data-dir", default=str(Path(__file__).parent / "data"), help="directory with *.yml files")
    parser.add_argument("--only", help="regex: process only files whose name matches")
    parser.add_argument("--skip", help="regex: skip files whose name matches")
    parser.add_argument("--list", action="store_true", help="list the selected files and exit")
    parser.add_argument("--validate", action="store_true", help="check the data files offline (no NetBox needed) and exit")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen; no writes")
    parser.add_argument("--cleanup", action="store_true",
                        help="delete the objects described by the files (reverse order); use on test instances only")
    parser.add_argument("--fail-fast", action="store_true", help="stop at the first failure")
    parser.add_argument("--insecure", action="store_true", help="do not verify the TLS certificate")
    parser.add_argument("--ca-bundle", help="CA bundle file for TLS verification")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every object")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir)
    files = select_files(data_dir, args.only, args.skip)
    if args.list:
        for path in files:
            print(path.name)
        return 0
    if not files:
        print(f"no data files selected in {data_dir}", file=sys.stderr)
        return 2
    if args.validate:
        return validate_files(files)
    if not args.url or not args.token:
        print("NETBOX_URL and NETBOX_TOKEN (or --url / --token) are required", file=sys.stderr)
        return 2

    nb = pynetbox.api(args.url.rstrip("/"), token=args.token)
    if args.insecure:
        nb.http_session.verify = False
    elif args.ca_bundle:
        nb.http_session.verify = args.ca_bundle
    try:
        print(f"Connected to NetBox {nb.version} at {args.url}")
    except Exception as exc:  # noqa: BLE001 - report any connection/auth problem plainly
        print(f"cannot connect to NetBox: {exc}", file=sys.stderr)
        return 2

    populator = Populator(nb, args.dry_run, args.verbose, args.fail_fast)
    if args.cleanup:
        files = list(reversed(files))
    total = Stats()
    for path in files:
        print(f"{path.name}")
        try:
            stats = populator.process_file(path, args.cleanup)
        except DataError as exc:
            print(f"  FAIL  {exc}")
            stats = Stats()
            stats.failed = 1
            if args.fail_fast:
                return 1
        print(f"  -> {stats.line(args.cleanup)}")
        total.add(stats)
    print(f"\nTOTAL: {total.line(args.cleanup)}")
    if args.dry_run and populator.unresolved_in_dry_run:
        print(f"(dry run: {populator.unresolved_in_dry_run} references point to objects that do not exist yet)")
    return 1 if total.failed else 0


if __name__ == "__main__":
    sys.exit(main())
