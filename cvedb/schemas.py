from abc import abstractmethod, ABC
from datetime import datetime, timezone
from sqlite3 import Connection
import sys
from typing import Any, Callable, Dict, Iterator, Optional, Tuple, Type, TypeVar, Union

from cvss import CVSS2, CVSS3, CVSSError

from .cve import Configurations, CVE, Description, Reference


SCHEMAS: Dict[int, Type["Schema"]] = {}


S = TypeVar("S", bound="Schema")

FEED_TABLE_CREATE = (
    "CREATE TABLE IF NOT EXISTS feeds("
    "name VARCHAR UNIQUE NOT NULL, "
    "last_modified INTEGER NULL, "
    "last_checked INTEGER NULL "
    ")"
)

CVE_TABLE_CREATE_V0 = (
    "CREATE TABLE IF NOT EXISTS cves("
    "id VARCHAR DESC NOT NULL, "
    "feed REFERENCES feeds (rowid) NOT NULL, "
    "published INTEGER NOT NULL, "
    "last_modified INTEGER NOT NULL, "
    "impact_vector VARCHAR NULL, "
    "base_score REAL NULL, "
    "severity INTEGER NOT NULL, "
    "PRIMARY KEY (id, feed)"
    ")"
)

CVE_TABLE_CREATE_V1 = (
    "CREATE TABLE IF NOT EXISTS cves("
    "id VARCHAR DESC NOT NULL, "
    "feed REFERENCES feeds (rowid) NOT NULL, "
    "published INTEGER NOT NULL, "
    "last_modified INTEGER NOT NULL, "
    "impact_vector VARCHAR NULL, "
    "base_score REAL NULL, "
    "severity INTEGER NOT NULL, "
    "configurations VARCHAR NULL, "
    "PRIMARY KEY (id, feed)"
    ")"
)

DESCRIPTIONS_TABLE_CREATE = (
    "CREATE TABLE IF NOT EXISTS descriptions("
    "cve REFERENCES cves (id) NOT NULL, "
    "lang VARCHAR NOT NULL DEFAULT \"en\", "
    "description VARCHAR NOT NULL"
    ")"
)

REFERENCES_TABLE_CREATE = (
    "CREATE TABLE IF NOT EXISTS refs("
    "cve REFERENCES cves (id) NOT NULL, "
    "name VARCHAR NULL, "
    "url VARCHAR NULL"
    ")"
)


CPES_TABLE_CREATE = (
    "CREATE TABLE IF NOT EXISTS cpes("
    "part VARCHAR NOT NULL, "
    "vendor VARCHAR NULL, "
    "product VARCHAR NULL, "
    "version VARCHAR NULL, "
    "update_str VARCHAR NULL, "
    "edition VARCHAR NULL, "
    "language VARCHAR NULL, "
    "sw_edition VARCHAR NULL, "
    "target_sw VARCHAR NULL, "
    "other VARCHAR NULL"
    ")"
)


CONFIGURATIONS_TABLE_CREATE = (
    "CREATE TABLE IF NOT EXISTS configurations("
    "cpe REFERENCES cpes(rowid) NOT NULL, "
    "cve REFERENCES cves(id) NOT NULL, "
    "PRIMARY KEY (cpe, cve)"
    ")"
)


def register_schema(version: int):
    def decorator(cls):
        if version in SCHEMAS:
            raise TypeError(f"Schema version {version} is already registered to class {SCHEMAS[version].__name__}")
        SCHEMAS[version] = cls
        setattr(cls, "version", version)
        return cls
    return decorator


class Schema(ABC):
    version: int

    def __init__(self, connection: Connection):
        self.connection: Connection = connection

    def path(self) -> str:
        for _, name, filename in self.connection.execute("PRAGMA database_list"):
            if name == "main" and filename is not None:
                return filename
        raise ValueError(f"Unknown database path for connection {self.connection}")

    @staticmethod
    def open(connection: Connection) -> "Schema":
        c = connection.cursor()
        c.execute("PRAGMA user_version")
        schema_version: int = c.fetchone()[0]
        latest_version = Schema.latest()
        if schema_version not in SCHEMAS:
            raise ValueError(f"Database is using schema version {schema_version}, "
                             f"but expected at most {Schema.latest().version}")
        elif schema_version == 0:
            # see if this is just a blank database
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cves'")
            if len(c.fetchall()) == 0:
                # this is a blank database, so start from scratch with the latest version
                return latest_version.create(connection)
            # this is actually a database version 0
        schema = SCHEMAS[schema_version]
        if schema.version < latest_version.version:
            if sys.stderr.isatty() and sys.stdin.isatty():
                sys.stderr.write(f"The database is currently in version {schema_version}, but the latest version is "
                                 f"{latest_version.version}.\n")
                while True:
                    sys.stderr.write("Would you like to upgrade the database? [Yn] ")
                    sys.stderr.flush()
                    choice = input().lower()
                    if choice in ("y", "n", ""):
                        break
                if choice != "n":
                    return latest_version.migrate(schema(connection))
        return schema(connection)

    @classmethod
    @abstractmethod
    def migrate_from_previous(cls: Type[S], previous_schema: "Schema") -> S:
        raise NotImplementedError()

    @classmethod
    def migrate(cls: Type[S], older_schema: "Schema") -> S:
        if older_schema.version > cls.version:
            raise ValueError(f"Schema {older_schema} is newer than {cls.__name}")
        while older_schema.version < cls.version:
            prev_version = older_schema
            older_schema = older_schema.next_version().migrate_from_previous(older_schema)
            if prev_version is older_schema:
                # the migration failed, so return a best effort
                return older_schema
        return older_schema

    @classmethod
    @abstractmethod
    def create(cls: Type[S], connection: Connection) -> S:
        raise NotImplementedError()

    @classmethod
    def prior_version(cls) -> Optional[Type["Schema"]]:
        if cls.version == 0:
            return None
        else:
            return SCHEMAS[cls.version - 1]

    @classmethod
    def next_version(cls) -> Optional[Type["Schema"]]:
        return SCHEMAS.get(cls.version + 1, None)

    @staticmethod
    def latest() -> Type["Schema"]:
        return SCHEMAS[max(SCHEMAS.keys())]

    @abstractmethod
    def feed_id(self, name: str) -> int:
        raise NotImplementedError()

    @abstractmethod
    def add(self, cve: CVE, source_feed: int):
        raise NotImplementedError()

    @abstractmethod
    def cve_iter(self, rows: Iterator[Tuple[Union[float, int, str], ...]]) -> Iterator[CVE]:
        raise NotImplementedError()


@register_schema(0)
class SchemaV0(Schema):
    @classmethod
    def create(cls: Type[S], connection: Connection, cve_table_create=CVE_TABLE_CREATE_V0) -> S:
        connection.execute(FEED_TABLE_CREATE)
        connection.execute(cve_table_create)
        connection.execute(DESCRIPTIONS_TABLE_CREATE)
        connection.execute("PRAGMA user_version = 0")
        return cls(connection)

    def feed_id(self, name: str) -> int:
        c = self.connection.cursor()
        c.execute(f"INSERT OR IGNORE INTO feeds (name) VALUES (?)", (name,))
        if c.lastrowid is not None and c.lastrowid > 0:
            return c.lastrowid
        else:
            c.execute("SELECT rowid FROM feeds WHERE name = ?", (name,))
            return c.fetchone()[0]

    @classmethod
    def migrate_from_previous(cls, previous_schema: Schema) -> "SchemaV0":
        raise ValueError("Schema version 0 has no previous version from which to migrate.")

    def add(self, cve: CVE, source_feed: int, **extra_cols):
        if cve.impact is None:
            impact_vector = None
            base_score = None
        else:
            impact_vector = cve.impact.vector
            base_score = float(cve.impact.base_score)
        with self.connection as c:
            col_names = []
            col_values = []
            for name, value in extra_cols.items():
                col_names.append(name)
                col_values.append(value)
            extra_col_names = "".join(f", {col}" for col in col_names)
            c.execute(
                "INSERT OR REPLACE INTO cves "
                f"(id, feed, published, last_modified, impact_vector, base_score, severity{extra_col_names}) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?{', ?' * len(extra_cols)})", [
                    cve.cve_id, source_feed, cve.published_date.astimezone().timestamp(),
                    cve.last_modified_date.astimezone().timestamp(), impact_vector, base_score, int(cve.severity)
                ] + col_values
            )
            for description in cve.descriptions:
                c.execute(
                    "INSERT OR REPLACE INTO descriptions "
                    "(cve, lang, description) "
                    "VALUES (?, ?, ?)", (
                        cve.cve_id, description.lang, description.value
                    )
                )

    def cve_iter(
            self,
            rows: Iterator[Tuple[Union[float, int, str], ...]],
            extra_row_handler: Callable[[Tuple[Union[float, int, str], ...], Dict[str, Any]], Any] = lambda *_:None
    ) -> Iterator[CVE]:
        for cve_id, _, published, last_modified, impact_vector, *extra_rows in rows:
            if impact_vector is None:
                impact = None
            else:
                try:
                    impact = CVSS3(impact_vector)
                except CVSSError:
                    try:
                        impact = CVSS2(impact_vector)
                    except CVSSError:
                        impact = None
            d = self.connection.cursor()
            d.execute(f"SELECT lang, description FROM descriptions WHERE cve = ?", (cve_id,))
            descriptions = tuple(Description(lang, desc) for lang, desc in d.fetchall())
            kwargs = {}
            if extra_rows:
                extra_row_handler(extra_rows, kwargs)
            yield CVE(
                cve_id=cve_id,
                published_date=datetime.fromtimestamp(published, timezone.utc),
                last_modified_date=datetime.fromtimestamp(last_modified, timezone.utc),
                impact=impact,
                descriptions=descriptions,
                references=(),  # References are implemented in SchemaV1
                assigner=None,
                **kwargs
            )


@register_schema(1)
class SchemaV1(SchemaV0):
    @classmethod
    def create(cls, connection: Connection) -> "SchemaV1":
        super().create(connection, cve_table_create=CVE_TABLE_CREATE_V1)
        connection.execute(REFERENCES_TABLE_CREATE)
        connection.execute(CPES_TABLE_CREATE)
        connection.execute(CONFIGURATIONS_TABLE_CREATE)
        connection.execute("PRAGMA user_version = 1")
        return cls(connection)

    @classmethod
    def migrate_from_previous(cls: Type[S], previous_schema: SchemaV0) -> S:
        message = "There is no way to migrate from schema version 0 to version 1 without re-downloading all CVEs."
        if not sys.stdin.isatty():
            try:
                path = previous_schema.path()
            except ValueError:
                path = "the database file"
            if sys.stderr.isatty():
                sys.stderr.write(f"Warning: {message} Continuing with schema version 0. "
                                 f"To force an upgrade to version 1, delete {path}.\n")
                return previous_schema
            else:
                raise ValueError(f"{message} To force an upgrade to version 1, delete {path}.")
        sys.stderr.write(f"{message}\n")
        while True:
            sys.stderr.write("Upgrade to schema version 1 and re-download all CVEs? [Yn] ")
            choice = input().lower()
            if choice in ("y", "n", ""):
                break
        if choice == "n":
            return previous_schema
        previous_schema.connection.execute("DROP TABLE IF EXISTS cves")
        previous_schema.connection.execute("DROP TABLE IF EXISTS feeds")
        previous_schema.connection.execute("DROP TABLE IF EXISTS descriptions")
        return SchemaV1.create(previous_schema.connection)

    def add(self, cve: CVE, source_feed: int, **extra_cols):
        if "configurations" not in extra_cols:
            extra_cols["configurations"] = cve.configurations.dumps()
        super().add(cve, source_feed, **extra_cols)
        for ref in cve.references:
            self.connection.execute(
                "INSERT OR REPLACE INTO refs "
                "(cve, name, url) "
                "VALUES (?, ?, ?)", (
                    cve.cve_id, ref.name, ref.url
                )
            )
        c = self.connection.cursor()
        cols = ("part", "vendor", "product", "version", "update_str", "edition", "language", "sw_edition", "target_sw",
                "other")
        col_names = ", ".join(cols)
        for cpe in cve.configurations.vulnerable_cpes():
            c.execute(
                "INSERT OR IGNORE INTO cpes ("
                f"{col_names}"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ",
                [str(c) for c in
                    (cpe.part.value, cpe.vendor, cpe.product, cpe.version, cpe.update, cpe.edition, cpe.lang,
                     cpe.sw_edition, cpe.target_sw, cpe.other)]
            )
            if c.lastrowid is None:
                # the CPE already existed
                args = " AND ".join(f"{col} = ?" for col in cols)
                c.execute(f"SELECT rowid FROM cpes WHERE {args}", (cpe.part.value, cpe.vendor, cpe.product, cpe.version,
                                                                   cpe.update, cpe.edition, cpe.lang,
                                                                   cpe.sw_edition, cpe.target_sw, cpe.other))
                cpe_row = c.fetchone()[0]
            else:
                cpe_row = c.lastrowid
            c.execute("INSERT OR REPLACE INTO configurations (cpe, cve) VALUES (?, ?)", (cpe_row, cve.cve_id))

    def cve_iter(
            self,
            rows: Iterator[Tuple[Union[float, int, str], ...]],
            extra_row_handler: Callable[[Tuple[Union[float, int, str], ...], Dict[str, Any]], Any] = lambda *_: None
    ) -> Iterator[CVE]:
        def handle_configurations(extra_rows: Tuple[Union[float, int, str], ...], kwargs: Dict[str, Any]):
            _, _, configurations, *extra_rows = extra_rows
            kwargs["configurations"] = Configurations.loads(configurations)
            if extra_rows:
                extra_row_handler(extra_rows, kwargs)

        for cve in super().cve_iter(rows, extra_row_handler=handle_configurations):
            d = self.connection.cursor()
            d.execute(f"SELECT url, name FROM refs WHERE cve = ?", (cve.cve_id,))
            references = tuple(Reference(url, name) for url, name in d.fetchall())
            if references:
                yield CVE(
                    cve_id=cve.cve_id,
                    published_date=cve.published_date,
                    last_modified_date=cve.last_modified_date,
                    impact=cve.impact,
                    descriptions=cve.descriptions,
                    references=references,
                    assigner=cve.assigner,
                    configurations=cve.configurations
                )
            else:
                yield cve
