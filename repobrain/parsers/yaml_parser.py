"""Generic YAML extraction with GitHub Actions, Compose, and Kubernetes adapters."""
from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator

import yaml
from yaml.nodes import MappingNode, Node as YamlNode, ScalarNode, SequenceNode

from .base import ParseResult, Parser
from ..graph.schema import Edge, EdgeType, FtsRow, Node, NodeType


_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_ENV_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(?::?[-?])[^}]*)?\}")


def _walk(node: YamlNode, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], YamlNode]]:
    if isinstance(node, MappingNode):
        for key, value in node.value:
            key_text = key.value if isinstance(key, ScalarNode) else "?"
            child = (*path, str(key_text))
            yield child, value
            yield from _walk(value, child)
    elif isinstance(node, SequenceNode):
        for index, value in enumerate(node.value):
            child = (*path, str(index))
            yield from _walk(value, child)


def _line(lines: dict[tuple[str, ...], int], path: tuple[str, ...]) -> int | None:
    return lines.get(path)


class YamlAdapter:
    def matches(self, path: str, data: object) -> bool:
        return False

    def adapt(self, parser: "YamlParser", path: str, data: object,
              lines: dict[tuple[str, ...], int], result: ParseResult) -> None:
        raise NotImplementedError


class GitHubActionsAdapter(YamlAdapter):
    def matches(self, path: str, data: object) -> bool:
        return path.startswith(".github/workflows/") and isinstance(data, dict)

    def adapt(self, parser, path, data, lines, result):
        workflow = parser.entity(result, NodeType.GITHUB_WORKFLOW,
                                 str(data.get("name") or posixpath.basename(path)), path, path, 1)
        jobs = data.get("jobs") or {}
        if not isinstance(jobs, dict):
            return
        for job_name, job_data in jobs.items():
            jp = ("jobs", str(job_name))
            job = parser.entity(result, NodeType.GITHUB_JOB, str(job_name), f"{path}:{job_name}",
                                path, _line(lines, jp))
            parser.edge(result, EdgeType.CONTAINS, workflow, job, path, job.start_line)
            if not isinstance(job_data, dict):
                continue
            parser.extract_env(result, job, job_data.get("env"), path, (*jp, "env"), lines)
            for index, step_data in enumerate(job_data.get("steps") or []):
                if not isinstance(step_data, dict):
                    continue
                sp = (*jp, "steps", str(index))
                name = str(step_data.get("name") or step_data.get("uses") or f"step {index + 1}")
                step = parser.entity(result, NodeType.GITHUB_STEP, name, f"{path}:{job_name}:step:{index}",
                                     path, _line(lines, sp) or _line(lines, (*sp, "name")),
                                     {k: step_data[k] for k in ("run", "uses") if k in step_data})
                parser.edge(result, EdgeType.CONTAINS, job, step, path, step.start_line)
                parser.extract_env(result, step, step_data.get("env"), path, (*sp, "env"), lines)


class DockerComposeAdapter(YamlAdapter):
    def matches(self, path: str, data: object) -> bool:
        name = posixpath.basename(path).lower()
        return isinstance(data, dict) and isinstance(data.get("services"), dict) and (
            "compose" in name or "services" in data
        )

    def adapt(self, parser, path, data, lines, result):
        services = data.get("services") or {}
        made: dict[str, Node] = {}
        for service_name, spec in services.items():
            sp = ("services", str(service_name))
            service = parser.entity(result, NodeType.DOCKER_SERVICE, str(service_name),
                                    f"{path}:{service_name}", path, _line(lines, sp))
            made[str(service_name)] = service
            if not isinstance(spec, dict):
                continue
            if spec.get("image"):
                image_name = str(spec["image"])
                image = parser.entity(result, NodeType.DOCKER_IMAGE, image_name, image_name, "",
                                      _line(lines, (*sp, "image")))
                parser.edge(result, EdgeType.USES_IMAGE, service, image, path, image.start_line)
            if spec.get("build") is not None:
                service.metadata["build"] = spec["build"]
            for field in ("ports", "volumes", "command"):
                if field in spec:
                    service.metadata[field] = spec[field]
            parser.extract_env(result, service, spec.get("environment"), path, (*sp, "environment"), lines)
            parser.extract_interpolations(result, service, spec, path, service.start_line)
        for service_name, spec in services.items():
            if not isinstance(spec, dict):
                continue
            depends = spec.get("depends_on") or []
            names = depends.keys() if isinstance(depends, dict) else depends
            for dependency in names if isinstance(names, (list, dict, type({}.keys()))) else []:
                if str(dependency) in made:
                    parser.edge(result, EdgeType.DEPENDS_ON, made[str(service_name)], made[str(dependency)],
                                path, _line(lines, ("services", str(service_name), "depends_on")))


class KubernetesAdapter(YamlAdapter):
    def matches(self, path: str, data: object) -> bool:
        return isinstance(data, dict) and bool(data.get("apiVersion")) and bool(data.get("kind"))

    def adapt(self, parser, path, data, lines, result):
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        kind = str(data.get("kind"))
        name = str(metadata.get("name") or kind)
        resource = parser.entity(result, NodeType.KUBERNETES_RESOURCE, name,
                                 f"{kind}/{name}", path, _line(lines, ("kind",)),
                                 {"kind": kind, "apiVersion": data.get("apiVersion"),
                                  "namespace": metadata.get("namespace")})
        containers = (((data.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers")
        if containers is None and kind in {"Pod", "Job", "CronJob"}:
            spec = data.get("spec") or {}
            if kind == "Job": spec = (spec.get("template") or {}).get("spec") or {}
            if kind == "CronJob": spec = ((((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {})
            containers = spec.get("containers")
        for index, container in enumerate(containers or []):
            if not isinstance(container, dict): continue
            image_name = container.get("image")
            if image_name:
                image = parser.entity(result, NodeType.DOCKER_IMAGE, str(image_name), str(image_name), "",
                                      None)
                parser.edge(result, EdgeType.USES_IMAGE, resource, image, path, resource.start_line)
            parser.extract_k8s_env(result, resource, container.get("env"), path, resource.start_line)


class YamlParser(Parser):
    name = "yaml_parser"

    def __init__(self, adapters: list[YamlAdapter] | None = None):
        self.adapters = adapters or [GitHubActionsAdapter(), DockerComposeAdapter(), KubernetesAdapter()]

    def can_parse(self, path: str, language: str | None) -> bool:
        return language == "yaml" or posixpath.splitext(path)[1].lower() in {".yaml", ".yml"}

    def parse(self, path: str, content: str) -> ParseResult:
        result = ParseResult()
        config = self.entity(result, NodeType.CONFIG_FILE, posixpath.basename(path), path, path, 1,
                            {"format": "yaml"})
        result.fts_rows.append(FtsRow(path=path, name=config.name, content=content, node_id=config.id))
        try:
            composed = list(yaml.compose_all(content))
            documents = list(yaml.safe_load_all(content))
        except yaml.YAMLError as exc:
            result.warnings.append(f"{path}: invalid YAML: {exc}")
            return result
        for doc_index, (root, data) in enumerate(zip(composed, documents)):
            if root is None or data is None:
                continue
            line_map = {p: n.start_mark.line + 1 for p, n in _walk(root)}
            prefix = (str(doc_index),) if len(documents) > 1 else ()
            keys: dict[tuple[str, ...], Node] = {}
            for key_path, value_node in _walk(root):
                # Sequence indices locate values but are not config keys.
                if key_path[-1].isdigit():
                    continue
                qualified = ".".join((*prefix, *key_path))
                value = value_node.value if isinstance(value_node, ScalarNode) else None
                key = self.entity(result, NodeType.CONFIG_KEY, key_path[-1], qualified, path,
                                  value_node.start_mark.line + 1,
                                  {"value": value, "value_type": type(value).__name__})
                keys[key_path] = key
                parent_path = key_path[:-1]
                parent = keys.get(parent_path, config)
                self.edge(result, EdgeType.DECLARES_CONFIG if parent is config else EdgeType.CONTAINS,
                          parent, key, path, key.start_line)
                if value:
                    self.extract_interpolations(result, key, value, path, key.start_line)
            for adapter in self.adapters:
                if adapter.matches(path, data):
                    adapter.adapt(self, path, data, line_map, result)
        return result

    def entity(self, result, type_, name, qualified, path, line, metadata=None):
        node = Node(type=type_, name=name, qualified_name=qualified, path=path, start_line=line,
                    end_line=line, language="yaml", metadata=metadata or {}, extractor=self.name)
        result.nodes.append(node)
        return node

    def edge(self, result, type_, source, target, path, line, metadata=None):
        result.edges.append(Edge(type=type_, source_node_id=source.id, target_node_id=target.id,
                                 path=path, start_line=line, metadata=metadata or {}, extractor=self.name))

    def env(self, result, owner, name, path, line, value=None, metadata=None):
        env = Node(type=NodeType.ENV_VAR, name=name, qualified_name=name, path="", extractor=self.name)
        result.nodes.append(env)
        meta = {"value": value}
        meta.update(metadata or {})
        self.edge(result, EdgeType.SETS_ENV, owner, env, path, line, meta)

    def extract_env(self, result, owner, env_data, path, key_path, lines):
        if isinstance(env_data, dict):
            for name, value in env_data.items():
                if _ENV_NAME.match(str(name)):
                    self.env(result, owner, str(name), path, _line(lines, (*key_path, str(name))), value)
        elif isinstance(env_data, list):
            for index, item in enumerate(env_data):
                if isinstance(item, str):
                    name, _, value = item.partition("=")
                    if _ENV_NAME.match(name):
                        self.env(result, owner, name, path, _line(lines, (*key_path, str(index))), value or None)

    def extract_interpolations(self, result, owner, value, path, line):
        text = str(value)
        for name in _ENV_INTERPOLATION.findall(text):
            self.env(result, owner, name, path, line, metadata={"interpolation": text})

    def extract_k8s_env(self, result, owner, env_data, path, line):
        for entry in env_data or []:
            if not isinstance(entry, dict) or not _ENV_NAME.match(str(entry.get("name", ""))): continue
            name = str(entry["name"])
            value_from = entry.get("valueFrom") or {}
            secret = value_from.get("secretKeyRef") if isinstance(value_from, dict) else None
            metadata = {"value_from": value_from} if value_from else {}
            self.env(result, owner, name, path, line, entry.get("value"), metadata)
            if isinstance(secret, dict) and secret.get("name"):
                ref_name = str(secret["name"])
                ref = self.entity(result, NodeType.SECRET_REF, ref_name, ref_name, path, line,
                                  {"key": secret.get("key")})
                self.edge(result, EdgeType.REFERENCES, owner, ref, path, line)
