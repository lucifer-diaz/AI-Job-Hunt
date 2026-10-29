"""Versioned evaluation datasets, in LangSmith's Example shape.

A dataset is one JSON file under tests/fixtures/agent_eval/ (or an existing suite's own folder):

    {"name": "...", "version": 3, "description": "...", "examples": [{"key", "inputs", "outputs", "metadata"}]}

`version` is bumped by hand whenever a case is added, removed or relabelled; the report also records the file's
sha256, so a result always names the exact cases it was measured on. Ground truth is written by a person, never by a
model, and every case is synthetic: no real posting, resume, email or page.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
AGENT_EVAL = FIXTURES / "agent_eval"


@dataclass
class Example:
    """A LangSmith Example."""

    key: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    def langsmith(self, dataset: str, version: int) -> dict[str, Any]:
        return {"inputs": self.inputs, "outputs": self.outputs,
                "metadata": {**self.metadata, "key": self.key, "dataset": dataset, "dataset_version": version}}


@dataclass
class Dataset:
    name: str
    version: int
    sha256: str
    path: Path
    examples: list[Example]
    fixture: dict[str, Any] = field(default_factory=dict)  # shared inputs every example of the suite uses

    def identity(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "sha256": self.sha256, "examples": len(self.examples),
                "path": str(self.path.relative_to(FIXTURES.parent.parent))}


def load(path: Path) -> Dataset:
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data.get("version"), int):
        raise TypeError(f"{path.name}: a dataset needs an integer version")
    keys = [c["key"] for c in data.get("examples") or []]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{path.name}: duplicate example keys")
    return Dataset(name=data.get("name") or path.stem, version=data["version"],
                   sha256=hashlib.sha256(raw).hexdigest(), path=path,
                   examples=[Example(c["key"], c.get("inputs") or {}, c.get("outputs") or {}, c.get("metadata") or {})
                             for c in data.get("examples") or []],
                   fixture=data.get("fixture") or {})


def suite_dataset(name: str) -> Dataset:
    return load(AGENT_EVAL / f"{name}.json")
