"""Index management for Firestore Enterprise / MongoDB.

The declaration comes from the project's ``index.yaml`` — the same file the Datastore already required and that
``SkelModule`` hands through to the admin. One entry is a Mongo key in declaration order (equality fields, then
sort fields); ``_id`` is appended as the last field, in the direction of the field before it, exactly like
``utils.to_mongo_sort`` appends its tiebreaker. An entry whose only property is ``__key__``/``_id`` therefore
declares the plain ``_id`` index ``(("_id", 1),)`` — the one a query without an explicit order needs. MongoDB
brings that index along as ``_id_``; Firestore Enterprise creates none by itself, so there it has to be
declared like any other.

``createIndex`` blocks until the index is built — on Firestore Enterprise between a fraction of a second and a
measured 75-120 s per index, depending on the collection — so ``apply()`` belongs in a task or a script, never
in a request. ``setup()`` runs ``check_on_startup()``, which only warns; with
``conf.db.create_indexes_on_startup`` (default ``True``) the startup task ``tasks.create_missing_indexes`` then
has what is missing created, in a task of its own or, on the development server, in a background thread. On
Firestore Enterprise creating an index needs the permission ``datastore.indexes.create`` (role
``roles/datastore.indexAdmin``); ``roles/datastore.user`` is not enough.

``eligible()`` is the basis of the sort elision in ``transport.run_single_filter``: an index yields the viur
order when its leading keys are, as a set, the equality fields of the filter and the rest is exactly that order.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import typing as t

import pymongo
import yaml

from viur.core.config import conf


IndexSpec: t.TypeAlias = tuple[tuple[str, int], ...]

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_declared: dict[str, list[IndexSpec]] | None = None
_existing: dict[str, tuple[float, list[IndexSpec]]] = {}
_suggested: set[tuple] = set()

_DIRECTIONS: t.Final[dict[str | None, int]] = {None: 1, "asc": 1, "ascending": 1, "desc": -1, "descending": -1}

#: Indexes of the core kinds, in the same format as an ``index.yaml`` entry;
#: ``declared()`` merges them with the project's file.
CORE_INDEXES: t.Final[list[dict]] = [
    # viur-relations — touched by every save/delete involving a RelationalBone
    {"kind": "viur-relations", "properties": [
        {"name": "viur_src_kind"}, {"name": "viur_dest_kind"}, {"name": "viur_src_property"}, {"name": "src.__key__"},
    ]},
    {"kind": "viur-relations", "properties": [
        {"name": "viur_src_kind"}, {"name": "src.__key__"}, {"name": "viur_src_property"},
    ]},
    {"kind": "viur-relations", "properties": [{"name": "dest.__key__"}, {"name": "viur_relational_consistency"}]},
    {"kind": "viur-relations", "properties": [
        {"name": "dest.__key__"}, {"name": "viur_relational_updateLevel"}, {"name": "viur_delayed_update_tag"},
    ]},
    # file & blobs
    {"kind": "file", "properties": [{"name": "dlkey"}, {"name": "creationdate"}]},
    {"kind": "file", "properties": [{"name": "dlkey"}]},
    {"kind": "file", "properties": [{"name": "parententry"}, {"name": "sortindex"}]},
    {"kind": "file", "properties": [{"name": "parententry"}]},
    {"kind": "file", "properties": [{"name": "name"}]},
    {"kind": "file", "properties": [{"name": "pending"}, {"name": "creationdate"}]},
    {"kind": "file_rootNode", "properties": [{"name": "parententry"}, {"name": "sortindex"}]},
    {"kind": "viur-deleted-files", "properties": [{"name": "dlkey"}]},
    {"kind": "viur-blob-locks", "properties": [{"name": "has_old_blob_references"}]},
    {"kind": "viur-blob-locks", "properties": [{"name": "active_blob_references"}]},
    # user, session, securitykey, ratelimit
    {"kind": "user", "properties": [{"name": "name.idx"}]},
    {"kind": "user", "properties": [{"name": "uid"}]},
    {"kind": "user", "properties": [{"name": "access"}]},
    {"kind": "user", "properties": [{"name": "login_key"}]},
    {"kind": "viur-session", "properties": [{"name": "user"}]},
    {"kind": "viur-session", "properties": [{"name": "lastseen"}]},
    {"kind": "viur-securitykey", "properties": [{"name": "viur_until"}]},
    {"kind": "viur-securitykey", "properties": [{"name": "viur_session"}]},
    {"kind": "viur-ratelimit", "properties": [{"name": "expires"}]},
    # everything else
    {"kind": "viur-emails", "properties": [{"name": "isSend"}, {"name": "creationDate"}]},
    {"kind": "viur-cache", "properties": [{"name": "path"}]},
    {"kind": "viur-cache", "properties": [{"name": "accessedEntries"}]},
    {"kind": "viur-translations", "properties": [{"name": "name"}]},
    {"kind": "viur-translations", "properties": [{"name": "translations_missing"}, {"name": "name"}]},
    {"kind": "viur-transactionmarker", "properties": [{"name": "creationdate"}]},
    {"kind": "viur-script-node", "properties": [{"name": "parententry"}, {"name": "sortindex"}]},
    {"kind": "viur-script-leaf", "properties": [{"name": "parententry"}, {"name": "sortindex"}]},
    {"kind": "viur-script-node", "properties": [{"name": "parententry"}, {"name": "name"}]},
    {"kind": "viur-script-node", "properties": [{"name": "path"}]},
    {"kind": "viur-script-leaf", "properties": [{"name": "path"}]},
    # Plain ``_id`` index for the kinds viur-core walks page by page with a cursor and without a filter of its
    # own: without it every page is a collection scan, as Firestore Enterprise creates no ``_id`` index itself.
    # ``_id`` values rise over time, so on a kind with a very high write rate this index can become a hotspot.
    {"kind": "viur-relations", "properties": [{"name": "__key__"}]},
    {"kind": "file", "properties": [{"name": "__key__"}]},
    {"kind": "viur-blob-locks", "properties": [{"name": "__key__"}]},
    # file.py: doCleanupDeletedFiles() pages through the whole kind without a filter
    {"kind": "viur-deleted-files", "properties": [{"name": "__key__"}]},
    {"kind": "viur-session", "properties": [{"name": "__key__"}]},
    {"kind": "viur-securitykey", "properties": [{"name": "__key__"}]},
    {"kind": "viur-ratelimit", "properties": [{"name": "__key__"}]},
    {"kind": "viur-transactionmarker", "properties": [{"name": "__key__"}]},
    {"kind": "viur-emails", "properties": [{"name": "__key__"}]},
    {"kind": "viur-cache", "properties": [{"name": "__key__"}]},
    # i18n.py: DatastoreSource.load() reads the whole kind with run(10_000)
    {"kind": "viur-translations", "properties": [{"name": "__key__"}]},
    # moduleconf.py: the module list reads the whole kind with run(999)
    {"kind": "viur-module-conf", "properties": [{"name": "__key__"}]},
]


def name(spec: IndexSpec) -> str:
    """The default name MongoDB gives an index: ``field_1_field2_-1``."""
    return "_".join(f"{field}_{direction}" for field, direction in spec)


def parse_spec(kind: str, properties: list[dict]) -> IndexSpec:
    """The Mongo key of one ``index.yaml`` entry; ``_id`` closes it as the tiebreaker.

    ``__key__``/``_id`` as the only property yields the plain ``_id`` index ``(("_id", 1),)``, or
    ``(("_id", -1),)`` with ``direction: desc`` — for a pass that iterates a whole kind descending.

    :param kind: The kind of the entry, for the error message.
    :param properties: The ``properties`` of the entry.
    :return: The index spec.
    :raises ValueError: On an unknown ``direction``.
    """
    keys: list[tuple[str, int]] = []
    for prop in properties:
        direction = prop.get("direction")
        if direction not in _DIRECTIONS:
            raise ValueError(f"index.yaml: kind {kind!r}, property {prop.get('name')!r}: "
                             f"unknown direction {direction!r} (allowed: asc, desc)")
        # index.yaml keeps the Datastore spelling so existing project files stay valid; the documents name the
        # field ``_id``. A query itself has to name ``_id``, ``utils.reject_legacy_key`` refuses ``__key__`` there.
        field = "_id" if prop["name"] == "__key__" else prop["name"].replace(".__key__", "._id")
        keys.append((field, _DIRECTIONS[direction]))
    if not keys or keys[-1][0] != "_id":
        keys.append(("_id", keys[-1][1] if keys else 1))
    return tuple(keys)


def declared() -> dict[str, list[IndexSpec]]:
    """The core indexes (``CORE_INDEXES``) plus the project's ``index.yaml``, deduplicated by index name.

    Loaded once per process. Without a file only the core indexes remain.

    :return: The declared index specs per kind.
    """
    global _declared
    with _lock:
        if _declared is not None:
            return _declared
    path = os.path.join(conf.instance.project_base_path, "index.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            entries = (yaml.safe_load(fh) or {}).get("indexes") or []
    except FileNotFoundError:
        logger.info(f"No index.yaml at {path!r} — no project indexes declared")
        entries = []
    result: dict[str, list[IndexSpec]] = {}
    seen: dict[str, set[str]] = {}
    for entry in [*CORE_INDEXES, *entries]:
        kind = entry["kind"]
        spec = parse_spec(kind, entry.get("properties") or [])
        if name(spec) in seen.setdefault(kind, set()):
            continue
        seen[kind].add(name(spec))
        result.setdefault(kind, []).append(spec)
    with _lock:
        _declared = result
    return result


def existing(kind: str) -> list[IndexSpec]:
    """The indexes the collection *kind* actually has, an ``_id`` index included.

    MongoDB reports its primary index as ``_id_``; Firestore Enterprise reports none, because it creates
    none — an ``_id`` index has to be declared there, and then appears under its own name (``_id_1``).

    Cached per kind for ``conf.db.index_cache_ttl`` seconds; the query itself runs outside the lock. An index
    that does not guarantee the same hit set and key order as a collection scan in the default BSON ordering is
    skipped: a ``sparse``, ``partialFilterExpression`` or ``hidden`` index (truthy) does not cover every
    document, an index with a direction outside ``1``/``-1`` (text/hashed/geo) is no sort prefix, and an index
    with a ``collation`` sorts by its rules instead of by BSON byte order. All three would break the promise
    that the sort elision never changes the result set or its order.

    :param kind: The kind whose collection is inspected.
    :return: The usable index specs of that collection.
    """
    from .transport import get_collection

    now = time.monotonic()
    with _lock:
        cached = _existing.get(kind)
        if cached and now - cached[0] < conf.db.index_cache_ttl:
            return list(cached[1])
    info = get_collection(kind).index_information()
    specs = []
    for entry in info.values():
        if entry.get("sparse") or entry.get("partialFilterExpression") or entry.get("hidden") or entry.get("collation"):
            continue
        if any(d not in (1, -1) for _, d in entry["key"]):
            continue
        specs.append(tuple((f, int(d)) for f, d in entry["key"]))
    with _lock:
        _existing[kind] = (now, specs)
    return list(specs)


def invalidate(kind: str | None = None) -> None:
    """Drop the cache of :func:`existing` — for *kind*, or for every kind on ``None``."""
    with _lock:
        if kind is None:
            _existing.clear()
        else:
            _existing.pop(kind, None)


def missing() -> dict[str, list[IndexSpec]]:
    """Declared but not present, per kind; a kind without a gap is left out of the result."""
    result = {}
    for kind, specs in declared().items():
        have = set(existing(kind))
        if lacking := [s for s in specs if s not in have]:
            result[kind] = lacking
    return result


LISTING_TIMEOUT: t.Final[int] = 30
"""Seconds one ``listIndexes`` of :func:`apply_part` may take. Under ``pymongo.timeout`` the driver would wait the
whole remaining deadline to select a server instead of its default 30 s, so an unreachable database held a task
for its entire deadline on the first kind."""

#: The work of :func:`apply_part`: ``[kind, specs, listed, retries]`` per entry, JSON-serialisable for a task
#: payload. Before its listing, *specs* are the wanted indexes of the kind; after it, the ones still to create.
#: *retries* counts the transient failures of the entry's next unit; a listing resets it, and an index that
#: failed goes on in an entry of its own.
IndexWork: t.TypeAlias = list[list]

MAX_RETRIES: t.Final[int] = 3
"""How often :func:`apply_part` tries a unit again after a transient error (:func:`transient`)."""


def transient(exc: Exception) -> bool:
    """Can *exc* pass by itself: a lost connection, an unreachable server, a timeout?"""
    return isinstance(exc, pymongo.errors.ConnectionFailure) or (
        isinstance(exc, pymongo.errors.PyMongoError) and exc.timeout
    )


def plan(kind: str | None = None, specs: list[IndexSpec] | None = None) -> IndexWork:
    """The work of creating the missing indexes — of every kind, of one kind, or the explicit *specs* of one kind.

    :param kind: The kind to work on, or ``None`` for every declared kind.
    :param specs: Explicit specs to create instead of the declared ones; requires *kind*.
    :return: The work for :func:`apply_part`.
    """
    if specs is not None:
        if kind is None:
            raise ValueError("apply(specs=...) needs a kind")
        return [[kind, list(specs), False, 0]]
    wanted = declared()
    if kind is not None:
        return [[kind, wanted[kind], False, 0]] if kind in wanted else []
    return [[k, specs, False, 0] for k, specs in wanted.items()]


def apply_part(work: IndexWork, *, time_budget: float | None = None) -> tuple[list[tuple[str, str, float]], IndexWork]:
    """Do *work* from :func:`plan` until it is done or *time_budget* is used up; return what is left.

    One unit is listing the indexes of a kind or creating one index. A unit that fails with a transient error
    (:func:`transient`) is left for a later call, up to ``MAX_RETRIES`` times, so a short outage of the database
    does not leave indexes missing. Any other failure, and the last transient one, is logged and the unit dropped,
    because a conflict with a second instance building the very same index must not keep the remaining indexes
    from being built. Without the permission ``datastore.indexes.create`` (role ``roles/datastore.indexAdmin`` on
    Firestore Enterprise) every index fails that way. The budget is checked before every unit but the first, so
    each call tries at least one unit, and a chain of calls ends after at most ``1 + MAX_RETRIES`` calls per unit.

    :param work: The work, possibly from a task payload (JSON lists instead of tuples).
    :param time_budget: Seconds, counted from the start of the call, after which no further unit starts. It does
        not cut a running unit short; wrap the call in ``pymongo.timeout`` for a hard deadline.
    :return: ``(kind, name, seconds)`` per created index, and the work left (empty when everything is done).
    """
    from .transport import get_collection

    started_all = time.monotonic()
    # An entry queued before the retry counter existed has three fields; *retries* then starts at 0.
    work = [
        [k, [tuple((field, int(d)) for field, d in spec) for spec in specs], listed, *(retries or [0])]
        for k, specs, listed, *retries in work
    ]
    later = []  # units that failed transiently in this call; trying them again right away would not help
    done = []
    units = 0
    while work:
        k, specs, listed, retries = work[0]
        if listed and not specs:
            invalidate(k)
            work.pop(0)
            continue
        if units and time_budget is not None and time.monotonic() - started_all >= time_budget:
            logger.info(f"Index budget of {time_budget:.0f}s used up — left: {describe(work + later)}")
            invalidate(k)
            return done, work + later
        units += 1
        if not listed:
            try:
                with pymongo.timeout(LISTING_TIMEOUT):
                    have = set(existing(k))
            except Exception as exc:  # noqa: BLE001 — one kind must not block the others
                work.pop(0)
                if transient(exc) and retries < MAX_RETRIES:
                    logger.warning(f"Indexes on {k!r} not listed, left for a later run "
                                   f"(retry {retries + 1}/{MAX_RETRIES}): {type(exc).__name__}: {exc}")
                    later.append([k, specs, False, retries + 1])
                else:
                    logger.error(f"Indexes on {k!r} not listed, its indexes skipped: {type(exc).__name__}: {exc}")
                continue
            work[0] = [k, [s for s in specs if s not in have], True, 0]
            continue
        spec = specs.pop(0)
        started = time.monotonic()
        try:
            created = get_collection(k).create_index(list(spec))
        except Exception as exc:  # noqa: BLE001 — one index must not block the others
            if transient(exc) and retries < MAX_RETRIES:
                logger.warning(f"Index {name(spec)!r} on {k!r} failed, left for a later run "
                               f"(retry {retries + 1}/{MAX_RETRIES}): {type(exc).__name__}: {exc}")
                later.append([k, [spec], True, retries + 1])
            else:
                logger.error(f"Index {name(spec)!r} on {k!r} not created: {type(exc).__name__}: {exc}")
            continue
        seconds = time.monotonic() - started
        logger.info(f"Index {created!r} on {k!r} created in {seconds:.1f}s")
        done.append((k, created, seconds))
    return done, later


def describe(work: IndexWork) -> str:
    """A short summary of *work* for the log."""
    return ", ".join(
        f"{k}: {len(specs)} index(es)" if listed else f"{k}: not listed yet" for k, specs, listed, *_ in work
    )


def apply(
    kind: str | None = None, specs: list[IndexSpec] | None = None, *, time_budget: float | None = None,
) -> list[tuple[str, str, float]]:
    """Create the missing indexes — of every kind, of one kind, or the explicit *specs* of one kind.

    Blocks per index (see the module docstring for how long); see :func:`apply_part` for how failures and the
    budget are handled. The units :func:`apply_part` leaves after a transient error are tried again here.

    :param kind: The kind to work on, or ``None`` for every declared kind.
    :param specs: Explicit specs to create instead of the declared ones; requires *kind*.
    :param time_budget: Seconds after which ``apply`` returns what it has created so far, leaving the rest to
        the next call.
    :return: ``(kind, name, seconds)`` per created index.
    """
    started = time.monotonic()
    work, done = plan(kind, specs), []
    while work:
        budget = None if time_budget is None else time_budget - (time.monotonic() - started)
        if budget is not None and budget <= 0:
            break
        part, work = apply_part(work, time_budget=budget)
        done += part
    return done


def equality_fields(flt: dict) -> frozenset[str]:
    """The fields of the Mongo filter *flt* holding a plain equality value — the index prefix.

    Looks into ``$and`` as well, where ``utils.to_mongo_filter`` puts the AND filters next to OR groups, and counts
    an operator dict with ``$eq`` (``=`` next to another operator on the same field).
    """
    # Neither another operator dict nor an ``$or`` counts; everything else is filtered residually.
    fields = set()
    for field, value in flt.items():
        if field == "$and":
            for part in value:
                fields |= equality_fields(part)
        elif not field.startswith("$") and (
            not (isinstance(value, dict) and any(k.startswith("$") for k in value)) or "$eq" in value
        ):
            fields.add(field)
    return frozenset(fields)


def eligible(kind: str, eq_fields: frozenset[str], sort: list[tuple[str, int]]) -> IndexSpec | None:
    """An existing index whose prefix is, as a set, *eq_fields* and whose rest is exactly *sort*.

    "Exactly" covers names, directions and length. The shortest one wins when several qualify.

    :param kind: The kind to look in.
    :param eq_fields: The equality fields the index has to carry as its prefix.
    :param sort: The Mongo sort the index has to yield.
    :return: The index spec, or ``None`` when none qualifies.
    """
    n = len(eq_fields)
    best = None
    for spec in existing(kind):
        if len(spec) != n + len(sort):
            continue
        if {f for f, _ in spec[:n]} != set(eq_fields) or len({f for f, _ in spec[:n]}) != n:
            continue
        if list(spec[n:]) != list(sort):
            continue
        if best is None or len(spec) < len(best):
            best = spec
    return best


def covers(kind: str, eq_fields: frozenset[str]) -> bool:
    """Is there an index whose leading keys are, as a set, exactly *eq_fields*?"""
    # Direction and further keys do not matter: for a lookup without an order that is enough for the planner
    # to search through the index instead of scanning.
    n = len(eq_fields)
    if n == 0:
        return True
    return any(len(spec) >= n and {f for f, _ in spec[:n]} == set(eq_fields) for spec in existing(kind))


def suggest(kind: str, eq_fields: frozenset[str], sort: list[tuple[str, int]]) -> None:
    """Development server only: log the ready-made ``index.yaml`` entry, once per query shape.

    A query sorted by ``_id`` alone is suggested as ``- name: __key__``: that declares the plain ``_id``
    index, which Firestore Enterprise accepts and which turns every cursor page of such a pass from a
    collection scan into an index scan. Nothing is suggested only when the query leaves nothing to declare —
    neither equality fields nor a sort, the lookup path.

    :param kind: The kind the query ran on.
    :param eq_fields: The equality fields of the filter.
    :param sort: The Mongo sort of the query.
    """
    if not conf.instance.is_dev_server:
        return
    properties = [f"  - name: {field}" for field in sorted(eq_fields)]
    for field, direction in sort:
        if field == "_id" and (eq_fields or len(sort) > 1):
            continue  # the loader appends it as the tiebreaker of the field before it
        properties.append(f"  - name: {'__key__' if field == '_id' else field}")
        if direction == -1:
            properties.append("    direction: desc")
    if not properties:
        return

    shape = (kind, tuple(sorted(eq_fields)), tuple(sort))
    with _lock:
        if shape in _suggested:
            return
        _suggested.add(shape)
    lines = [f"- kind: {kind}", "  properties:", *properties]
    logger.info("No matching index for this query — suggestion for index.yaml:\n" + "\n".join(lines))


def check_on_startup() -> None:
    """Report missing indexes as a warning; never creates one, never raises."""
    try:
        for kind, specs in missing().items():
            for spec in specs:
                logger.warning(f"Index {name(spec)!r} on {kind!r} is declared but missing — create it via "
                               f"the admin task 'viur-db-apply-indexes' or db.indexes.apply()")
    except Exception as exc:  # noqa: BLE001 — an index check must not keep the instance from starting
        logger.warning(f"Index check on startup skipped: {type(exc).__name__}: {exc}")
