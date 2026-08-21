"""Where a phase's tunables come from, and what a run actually used.

The sibling of `prompt_registry`, and for the same reason. A prompt in a file is
pinned by the commit; a prompt in a mutable row is editable but cannot explain a
past run, so rows are versioned and every run records the version it read. A
setting has exactly that problem in a sharper form: "why did this run escalate
to a stronger model and that one not" is unanswerable if the rule is a constant
someone has since edited, and equally unanswerable if it is a row someone has
since overwritten.

So settings resolve the same way prompts do — **database, then the built-in
default** — and resolution returns the value together with its provenance.

### Why not just read a constant

Constants are fine until the answer to "what did this run do" matters. This
project already had that failure: a shape check gated on an unrelated condition
silently skipped on the command line, and nothing in the output said so. A
setting that is resolved, announced and recorded cannot go quiet in that way.

### Defaults stay in code, deliberately

There is no profile layer here, and no requirement that a database exist. The
command-line tools run without one, and a pipeline whose *behaviour* depends on
a reachable database would be worse than one whose auditing does. Every
built-in default is a working configuration; the database only ever overrides.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SOURCE_DATABASE = "database"
SOURCE_DEFAULT = "built_in_default"


@dataclass
class ResolvedSetting:
    """One setting's value, and the record of where it came from."""

    process_name: str
    key: str
    value: Any = None
    source: str = SOURCE_DEFAULT
    version: Optional[int] = None
    description: str = ""

    @property
    def reproducible(self) -> bool:
        """A built-in default is pinned by the commit; a row needs its version."""
        return self.source != SOURCE_DATABASE or self.version is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "version": self.version,
            "reproducible": self.reproducible,
        }


@dataclass
class ResolvedSettings:
    """Every setting a process asked for, with per-key provenance.

    Behaves like a mapping for reading values, so callers that do not care where
    a value came from are not made to. `provenance()` is what a run records.
    """

    process_name: str
    resolved: Dict[str, ResolvedSetting] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.resolved[key].value

    def get(self, key: str, default: Any = None) -> Any:
        entry = self.resolved.get(key)
        return default if entry is None else entry.value

    @property
    def from_database(self) -> List[str]:
        return sorted(k for k, r in self.resolved.items() if r.source == SOURCE_DATABASE)

    def provenance(self) -> Dict[str, Any]:
        """The record a run stores: the values *and* where each came from.

        Values are recorded rather than referenced so the record survives the
        row changing or being deleted.
        """
        return {
            "process_name": self.process_name,
            "settings": {k: r.to_dict() for k, r in sorted(self.resolved.items())},
        }


def _active_row(db_session, process_name: str, key: str):
    from db.models import ProcessSetting

    return (
        db_session.query(ProcessSetting)
        .filter(ProcessSetting.process_name == process_name)
        .filter(ProcessSetting.key == key)
        .filter(ProcessSetting.active.is_(True))
        .order_by(ProcessSetting.version.desc())
        .first()
    )


def resolve_settings(
    process_name: str,
    defaults: Dict[str, Any],
    db_session=None,
) -> ResolvedSettings:
    """The settings for this process, each with its provenance.

    `defaults` is the complete set of keys the process understands; a key absent
    from it is not read from the database either, so a typo in a stored row
    cannot silently introduce a setting nothing honours.

    Never raises on a database problem. A registry that can take the pipeline
    down is worse than one that falls back to the defaults compiled into it, and
    the fallback is logged and named in the provenance rather than hidden.
    """
    out = ResolvedSettings(process_name=process_name)
    for key, value in defaults.items():
        out.resolved[key] = ResolvedSetting(
            process_name=process_name, key=key, value=value, source=SOURCE_DEFAULT
        )

    if db_session is None or not process_name:
        return out

    try:
        for key in defaults:
            row = _active_row(db_session, process_name, key)
            if row is None:
                continue
            # Stored as {"v": ...} so a JSON column can hold a bare bool or int
            # without the column type having to be a union.
            value = row.value.get("v") if isinstance(row.value, dict) else row.value
            out.resolved[key] = ResolvedSetting(
                process_name=process_name,
                key=key,
                value=value,
                source=SOURCE_DATABASE,
                version=row.version,
                description=row.description or "",
            )
    except Exception as e:
        logger.warning(
            f"Settings registry unavailable ({e}); using the built-in defaults for "
            f"{process_name!r}"
        )
        return resolve_settings(process_name, defaults, db_session=None)

    if out.from_database:
        logger.info(
            f"[{process_name}] settings from the database: {', '.join(out.from_database)}"
        )
    return out


def settings_for(process_name: str, defaults: Dict[str, Any], settings=None, db_session=None):
    """Resolve a process's settings unless the caller already did.

    Every phase that reads settings needs this same three-line preamble: use
    what I was handed, otherwise resolve, and never make a database mandatory.
    A caller passing a plain dict works too, which is what tests do.
    """
    if settings is not None:
        return settings
    return resolve_settings(process_name, defaults, db_session=db_session)


def register_setting(
    db_session,
    process_name: str,
    key: str,
    value: Any,
    description: str = "",
    created_by: str = "",
):
    """Store a new version of one setting and make it the active one.

    Never updates a row in place, for the same reason `register` does not: a run
    that recorded version 3 must still be able to read version 3 after version 4
    exists. This matters more for settings than for prompts, because a setting
    change is usually made *because* a run behaved unexpectedly, and the
    evidence is the old value.
    """
    from db.models import ProcessSetting

    existing = (
        db_session.query(ProcessSetting)
        .filter(ProcessSetting.process_name == process_name)
        .filter(ProcessSetting.key == key)
        .order_by(ProcessSetting.version.desc())
        .all()
    )
    for row in existing:
        row.active = False

    setting = ProcessSetting(
        process_name=process_name,
        key=key,
        version=(existing[0].version + 1) if existing else 1,
        active=True,
        value={"v": value},
        description=description,
        created_by=created_by,
    )
    db_session.add(setting)
    db_session.commit()
    logger.info(f"Registered {process_name}.{key} v{setting.version} = {value!r}")
    return setting


def setting_history(db_session, process_name: str, key: str) -> List[Dict[str, Any]]:
    """Every version of one setting, newest first."""
    from db.models import ProcessSetting

    rows = (
        db_session.query(ProcessSetting)
        .filter(ProcessSetting.process_name == process_name)
        .filter(ProcessSetting.key == key)
        .order_by(ProcessSetting.version.desc())
        .all()
    )
    return [
        {
            "version": r.version,
            "active": r.active,
            "value": r.value.get("v") if isinstance(r.value, dict) else r.value,
            "description": r.description,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "created_by": r.created_by,
        }
        for r in rows
    ]


def all_settings(db_session, process_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every stored setting row, for the command-line listing."""
    from db.models import ProcessSetting

    query = db_session.query(ProcessSetting)
    if process_name:
        query = query.filter(ProcessSetting.process_name == process_name)

    rows = query.order_by(
        ProcessSetting.process_name, ProcessSetting.key, ProcessSetting.version.desc()
    ).all()
    return [
        {
            "process_name": r.process_name,
            "key": r.key,
            "version": r.version,
            "active": r.active,
            "value": r.value.get("v") if isinstance(r.value, dict) else r.value,
            "description": r.description,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "created_by": r.created_by,
        }
        for r in rows
    ]
