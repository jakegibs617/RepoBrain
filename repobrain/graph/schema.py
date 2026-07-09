"""Graph schema: node/edge type enumerations, dataclasses, and deterministic IDs.

The type enumerations anticipate the full PRD data model (sections 11.1/11.2)
even though only a few types are produced by the current parsers.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum


class NodeType(StrEnum):
    REPOSITORY = "Repository"
    DIRECTORY = "Directory"
    FILE = "File"
    MODULE = "Module"
    PACKAGE = "Package"
    CLASS = "Class"
    FUNCTION = "Function"
    METHOD = "Method"
    VARIABLE = "Variable"
    TYPE = "Type"
    INTERFACE = "Interface"
    ROUTE = "Route"
    ENDPOINT = "Endpoint"
    CLI_COMMAND = "CLICommand"
    SCRIPT = "Script"
    WORKER = "Worker"
    QUEUE = "Queue"
    EVENT = "Event"
    DATABASE = "Database"
    TABLE = "Table"
    MIGRATION = "Migration"
    SCHEMA = "Schema"
    CONFIG_FILE = "ConfigFile"
    CONFIG_KEY = "ConfigKey"
    ENV_VAR = "EnvVar"
    SECRET_REF = "SecretRef"
    DOCKER_IMAGE = "DockerImage"
    DOCKER_SERVICE = "DockerService"
    KUBERNETES_RESOURCE = "KubernetesResource"
    GITHUB_WORKFLOW = "GitHubWorkflow"
    GITHUB_JOB = "GitHubJob"
    GITHUB_STEP = "GitHubStep"
    TEST_FILE = "TestFile"
    TEST_CASE = "TestCase"
    MARKDOWN_DOCUMENT = "MarkdownDocument"
    MARKDOWN_SECTION = "MarkdownSection"
    ADR = "ADR"
    DECISION = "Decision"
    CONCEPT = "Concept"
    TASK = "Task"
    AGENT_NOTE = "AgentNote"
    ASSUMPTION = "Assumption"
    OPEN_QUESTION = "OpenQuestion"


class EdgeType(StrEnum):
    CONTAINS = "CONTAINS"
    DEFINES = "DEFINES"
    IMPORTS = "IMPORTS"
    EXPORTS = "EXPORTS"
    CALLS = "CALLS"
    REFERENCES = "REFERENCES"
    IMPLEMENTS = "IMPLEMENTS"
    EXTENDS = "EXTENDS"
    INSTANTIATES = "INSTANTIATES"
    READS = "READS"
    WRITES = "WRITES"
    READS_ENV = "READS_ENV"
    SETS_ENV = "SETS_ENV"
    USES_CONFIG = "USES_CONFIG"
    DECLARES_CONFIG = "DECLARES_CONFIG"
    HANDLES_ROUTE = "HANDLES_ROUTE"
    EXPOSES_ENDPOINT = "EXPOSES_ENDPOINT"
    RUNS_COMMAND = "RUNS_COMMAND"
    INVOKES_SCRIPT = "INVOKES_SCRIPT"
    BUILDS_IMAGE = "BUILDS_IMAGE"
    USES_IMAGE = "USES_IMAGE"
    DEPLOYS = "DEPLOYS"
    STARTS_SERVICE = "STARTS_SERVICE"
    PUBLISHES_EVENT = "PUBLISHES_EVENT"
    CONSUMES_EVENT = "CONSUMES_EVENT"
    READS_TABLE = "READS_TABLE"
    WRITES_TABLE = "WRITES_TABLE"
    MIGRATES_TABLE = "MIGRATES_TABLE"
    DOCUMENTS = "DOCUMENTS"
    RATIONALE_FOR = "RATIONALE_FOR"
    MENTIONS = "MENTIONS"
    TESTS = "TESTS"
    COVERS = "COVERS"
    DEPENDS_ON = "DEPENDS_ON"
    MAY_IMPACT = "MAY_IMPACT"
    GENERATED_FROM = "GENERATED_FROM"
    OBSERVED_IN = "OBSERVED_IN"
    AUTHORED_BY_AGENT = "AUTHORED_BY_AGENT"


def node_id(type_: str, qualified_name_or_name: str, path: str) -> str:
    """Deterministic node id: sha1 over (type, qualified_name or name, path).

    Makes incremental reconciliation idempotent: re-parsing an unchanged
    entity always yields the same id, so upserts converge.
    """
    raw = "\x00".join((str(type_), qualified_name_or_name or "", path or ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def edge_id(type_: str, source_id: str, target_id: str, path: str, start_line: int | None) -> str:
    """Deterministic edge id: sha1 over (type, source_id, target_id, path, start_line)."""
    raw = "\x00".join((str(type_), source_id, target_id, path or "", str(start_line or 0)))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass
class Node:
    type: str
    name: str
    path: str
    qualified_name: str = ""
    start_line: int | None = None
    end_line: int | None = None
    language: str | None = None
    hash: str | None = None
    metadata: dict = field(default_factory=dict)
    confidence: float = 1.0
    extractor: str = ""
    commit_hash: str | None = None

    @property
    def id(self) -> str:
        return node_id(self.type, self.qualified_name or self.name, self.path)

    @property
    def metadata_json(self) -> str:
        return json.dumps(self.metadata, sort_keys=True)


@dataclass
class Edge:
    type: str
    source_node_id: str
    target_node_id: str
    path: str
    start_line: int | None = None
    end_line: int | None = None
    metadata: dict = field(default_factory=dict)
    confidence: float = 1.0
    extractor: str = ""
    commit_hash: str | None = None
    is_inferred: bool = False
    inference_reason: str | None = None

    @property
    def id(self) -> str:
        return edge_id(self.type, self.source_node_id, self.target_node_id, self.path, self.start_line)

    @property
    def metadata_json(self) -> str:
        return json.dumps(self.metadata, sort_keys=True)


@dataclass
class FtsRow:
    """A row destined for the content_fts full-text index."""

    path: str
    name: str
    content: str
