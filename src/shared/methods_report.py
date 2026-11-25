from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterable, Mapping

from shared.bibtex import parse_bibtex
from shared.persistence.atomic_write import write_text_atomic


PROVENANCE_SCHEMA_VERSION = 1

PCA_CITATION: dict[str, object] = {
    "id": "pearson1901pca", "role": "intermediate_method",
    "title": "On lines and planes of closest fit to systems of points in space",
    "authors": ["Pearson, Karl"], "year": 1901, "journal": "Philosophical Magazine",
    "volume": "2", "issue": "11", "pages": "559-572", "doi": "10.1080/14786440109462720",
}


@dataclass(frozen=True)
class CitationRecord:
    citation_id: str
    role: str
    title: str
    authors: tuple[str, ...] = ()
    year: str = ""
    container_title: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    doi: str = ""
    url: str = ""
    bibtex: str = ""
    required: bool = True
    note: str = ""
    entry_type: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, object], *, default_role: str = "method") -> "CitationRecord":
        authors = _authors(payload.get("authors") or payload.get("author"))
        supplied_bibtex = str(payload.get("bibtex") or "").strip()
        citation_id = str(payload.get("id") or payload.get("citation_id") or "").strip()
        if not citation_id and supplied_bibtex:
            match = re.search(r"@[A-Za-z]+\s*\{\s*([^,\s]+)", supplied_bibtex)
            citation_id = match.group(1) if match else "supplied-citation"
        return cls(
            citation_id=citation_id,
            role=str(payload.get("role") or default_role).strip(),
            title=str(payload.get("title") or payload.get("name") or citation_id).strip(),
            authors=authors,
            year=str(payload.get("year") or payload.get("date-released") or "").strip()[:4],
            container_title=str(payload.get("container_title") or payload.get("journal") or payload.get("booktitle") or "").strip(),
            volume=str(payload.get("volume") or "").strip(),
            issue=str(payload.get("issue") or payload.get("number") or "").strip(),
            pages=str(payload.get("pages") or payload.get("page") or "").strip(),
            doi=str(payload.get("doi") or "").strip().removeprefix("https://doi.org/"),
            url=str(payload.get("url") or "").strip(),
            bibtex=supplied_bibtex,
            required=bool(payload.get("required", True)),
            note=str(payload.get("note") or payload.get("notes") or "").strip(),
            entry_type=str(payload.get("entry_type") or payload.get("type") or "").strip(),
        )

    @property
    def key(self) -> str:
        if self.doi:
            return f"doi:{self.doi.casefold()}"
        if self.citation_id:
            return f"id:{self.citation_id.casefold()}"
        if self.url:
            return f"url:{self.url.rstrip('/').casefold()}"
        return f"title:{self.title.casefold()}|{self.authors}"

    def reference_text(self) -> str:
        author_text = ", ".join(self.authors)
        parts = [value for value in (author_text, f"({self.year})" if self.year else "", self.title) if value]
        text = " ".join(parts).rstrip(".") + "."
        if self.container_title:
            text += f" {self.container_title}"
            if self.volume:
                text += f" {self.volume}"
            if self.issue:
                text += f"({self.issue})"
            if self.pages:
                text += f", {self.pages}"
            text += "."
        if self.doi:
            text += f" https://doi.org/{self.doi}"
        elif self.url:
            text += f" {self.url}"
        if self.note:
            text += f" {self.note}"
        return text

    def to_bibtex(self) -> str:
        if self.bibtex:
            return self.bibtex.rstrip()
        key = _bibtex_key(self)
        fields = {
            "author": " and ".join(self.authors),
            "title": self.title,
            "year": self.year,
            "journal": self.container_title,
            "volume": self.volume,
            "number": self.issue,
            "pages": self.pages,
            "doi": self.doi,
            "url": self.url,
            "note": self.note,
        }
        kind = self.entry_type if self.entry_type in {"unpublished", "article", "misc"} else (
            "article" if self.container_title else "misc")
        lines = [f"@{kind}{{{key},"]
        for name, value in fields.items():
            if value:
                lines.append(f"  {name} = {{{_bibtex_escape(value)}}},")
        lines.append("}")
        return "\n".join(lines)


@dataclass(frozen=True)
class MethodsReport:
    title: str
    methods_text: str
    parameter_rows: tuple[tuple[str, str], ...]
    references: tuple[CitationRecord, ...]
    reproducibility_rows: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...] = ()
    scope: str = ""
    caption_summary: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": 1, **asdict(self)}

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "MethodsReport":
        if payload.get("schema_version") != 1:
            raise ValueError("Unsupported methods report version.")
        try:
            return cls(
                title=str(payload["title"]), methods_text=str(payload["methods_text"]),
                parameter_rows=tuple(tuple(row) for row in payload.get("parameter_rows", [])),
                references=tuple(CitationRecord.from_payload(row) for row in payload.get("references", [])),
                reproducibility_rows=tuple(tuple(row) for row in payload.get("reproducibility_rows", [])),
                warnings=tuple(payload.get("warnings", [])),
                scope=str(payload.get("scope") or ""),
                caption_summary=payload.get("caption_summary"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("The saved methods report is incomplete or unreadable.") from exc

    def _with_notes(self, text: str) -> str:
        if self.warnings:
            text += "\n\nProvenance notes: " + " ".join(self.warnings)
        return text

    def brief_text(self) -> str:
        return self._with_notes(self.methods_text)

    def caption_text(self) -> str:
        # Legacy frozen reports have no separate caption; retain their saved prose.
        return self._with_notes(self.caption_summary or self.methods_text)

    def references_text(self) -> str:
        return "\n\n".join(f"{index}. {citation.reference_text()}"
                           for index, citation in enumerate(self.publication_references(), 1))

    def publication_references(self) -> tuple[CitationRecord, ...]:
        """Use EIDORA's preferred citation without rewriting frozen analysis records."""
        return deduplicate_citations(
            CitationRecord.from_payload(eidora_citation_payload()) if citation.citation_id == "eidora" else citation
            for citation in self.references
        )

    @property
    def has_details(self) -> bool:
        return bool(self.parameter_rows or self.reproducibility_rows)

    @property
    def is_reference_only(self) -> bool:
        return not (self.methods_text or self.has_details or self.caption_summary)

    def single_reference(self, index: int) -> "MethodsReport":
        citation = self.publication_references()[index]
        return MethodsReport(title=f"{citation.title} — Citation", methods_text="", parameter_rows=(),
                             references=(citation,), reproducibility_rows=(),
                             scope=f"{citation.role.replace('_', ' ').capitalize()} reference")

    def brief_markdown(self, *, reference_links: bool = False) -> str:
        lines = ["## Brief methods", "", self.methods_text] if self.methods_text else []
        if self.caption_summary:
            lines.extend(["", "## Caption summary", "", self.caption_summary])
        if self.warnings:
            lines.extend(["", *[f"> **Provenance warning:** {warning}" for warning in self.warnings]])
        lines.extend(["", "## References", ""])
        citations = self.publication_references()
        for index, citation in enumerate(citations):
            link = f" [Open citation](eidora-reference:{index})" if reference_links and not self.is_reference_only else ""
            lines.extend([f"{index + 1}. {citation.reference_text()}{link}", ""])
        if not citations:
            lines.append("No references were recorded.")
        return "\n".join(lines).rstrip() + "\n"

    def details_markdown(self) -> str:
        lines = ["## Detailed methods", "", "| Setting | Value |", "|---|---|"]
        lines.extend(f"| {_markdown_cell(label)} | {_markdown_cell(_report_value(value))} |" for label, value in self.parameter_rows)
        lines.extend(["", "## Reproducibility details", "", "| Field | Value |", "|---|---|"])
        lines.extend(f"| {_markdown_cell(label)} | {_markdown_cell(_report_value(value))} |" for label, value in self.reproducibility_rows)
        return "\n".join(lines).rstrip() + "\n"

    def markdown(self, *, brief: bool = False) -> str:
        text = f"# {self.title}\n\n"
        if self.scope:
            text += f"Scope: {self.scope}\n\n"
        text += self.brief_markdown()
        if not brief and self.has_details:
            text += "\n" + self.details_markdown()
        return text

    def bibliography(self) -> str:
        if not self.references:
            return ""
        return "\n\n".join(citation.to_bibtex() for citation in self.publication_references()).rstrip() + "\n"


class MethodsReportExporter:
    @staticmethod
    def save_pair(destination: Path, report: MethodsReport, *, brief: bool = False) -> tuple[Path, Path]:
        markdown_path = Path(destination).with_suffix(".md")
        stem = markdown_path.name.removesuffix(".md")
        suffix = "-citation" if report.is_reference_only else "-methods"
        if not stem.endswith(suffix):
            markdown_path = markdown_path.with_name(f"{stem}{suffix}.md")
        bib_path = markdown_path.with_name(markdown_path.name.removesuffix(f"{suffix}.md") + "-references.bib")
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        previous_markdown = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else None
        previous_bib = bib_path.read_text(encoding="utf-8") if bib_path.exists() else None
        try:
            with tempfile.TemporaryDirectory(prefix=".eidora_methods_", dir=markdown_path.parent) as raw_stage:
                stage = Path(raw_stage)
                staged_markdown = stage / markdown_path.name
                staged_bib = stage / bib_path.name
                write_text_atomic(staged_markdown, report.markdown(brief=brief))
                write_text_atomic(staged_bib, report.bibliography())
                os.replace(staged_markdown, markdown_path)
                os.replace(staged_bib, bib_path)
        except Exception:
            if previous_markdown is None:
                markdown_path.unlink(missing_ok=True)
            else:
                write_text_atomic(markdown_path, previous_markdown)
            if previous_bib is None:
                bib_path.unlink(missing_ok=True)
            else:
                write_text_atomic(bib_path, previous_bib)
            raise
        return markdown_path, bib_path


def citation_payloads(value: object, *, default_role: str) -> list[dict[str, object]]:
    if isinstance(value, str):
        try:
            value = list(parse_bibtex(value))
        except ValueError:
            return []  # Legacy artifacts may contain unstructured citation text.
    values = value if isinstance(value, list) else [value]
    output: list[dict[str, object]] = []
    for raw in values:
        if not isinstance(raw, dict):
            continue
        payload = dict(raw)
        if isinstance(payload.get("author"), str) and "authors" not in payload:
            payload["authors"] = re.split(r"\s+and\s+", payload["author"])
        payload.setdefault("role", default_role)
        output.append(payload)
    return output


def deduplicate_citations(citations: Iterable[CitationRecord]) -> tuple[CitationRecord, ...]:
    output: list[CitationRecord] = []
    seen: set[str] = set()
    for citation in citations:
        key = citation.key
        if not citation.title or key in {"id:", "doi:"} or key in seen:
            continue
        seen.add(key)
        output.append(citation)
    return tuple(output)


def checksum_payload(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def eidora_citation_payload() -> dict[str, object]:
    """Read the repository citation authority without introducing a runtime dependency."""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    path = root / "CITATION.cff"
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    payload = dict(raw) if isinstance(raw, dict) else {}
    preferred = _mapping(payload.get("preferred-citation"))
    if preferred:
        return {**preferred, "id": "eidora", "role": "software", "required": True}
    return {
        "id": "eidora",
        "role": "software",
        "title": str(payload.get("title") or "EIDORA Desktop"),
        "authors": list(payload.get("authors") or [{"family-names": "Ohm", "given-names": "Tillmann"}]),
        "year": str(payload.get("date-released") or "")[:4],
        "url": str(payload.get("url") or "https://eidora.org"),
        "required": True,
    }


def _citation_records(value: object) -> list[CitationRecord]:
    values = value if isinstance(value, list) else []
    return [CitationRecord.from_payload(raw) for raw in values if isinstance(raw, dict)]


def _authors(value: object) -> tuple[str, ...]:
    values = value if isinstance(value, (list, tuple)) else ([value] if value else [])
    output: list[str] = []
    for author in values:
        if isinstance(author, dict):
            family = str(author.get("family-names") or author.get("family") or "").strip()
            given = str(author.get("given-names") or author.get("given") or "").strip()
            name = str(author.get("name") or "").strip() or ", ".join(value for value in (family, given) if value)
        else:
            name = str(author).strip()
        if name:
            output.append(name)
    return tuple(output)


def _bibtex_key(citation: CitationRecord) -> str:
    family = citation.authors[0].split(",", 1)[0] if citation.authors else citation.citation_id or "reference"
    raw = f"{family}{citation.year}{citation.citation_id}"
    return re.sub(r"[^A-Za-z0-9_-]+", "", raw) or "reference"


def _bibtex_escape(value: object) -> str:
    return str(value).replace("{", "\\{").replace("}", "\\}")


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(", ", ": "))
    return str(value)


def _humanize(value: object) -> str:
    return str(value).replace("_", " ").strip().capitalize()


def _implementation_text(method: Mapping[str, object]) -> str:
    name = str(method.get("implementation") or "not recorded")
    version = str(method.get("implementation_version") or "").strip()
    return f"{name} {version}".strip()


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def combine_reports(title: str, reports: Iterable[MethodsReport], *, warnings: Iterable[str] = ()) -> MethodsReport:
    parts = list(reports)
    return MethodsReport(
        title=f"{title} — Methods and Citations",
        methods_text="\n\n".join(dict.fromkeys(part.methods_text for part in parts if part.methods_text)),
        parameter_rows=tuple(row for part in parts for row in part.parameter_rows),
        references=deduplicate_citations(c for part in parts for c in part.references),
        reproducibility_rows=tuple(row for part in parts for row in part.reproducibility_rows),
        warnings=tuple(dict.fromkeys([*warnings, *(w for part in parts for w in part.warnings)])),
        caption_summary=("\n\n".join(dict.fromkeys(part.caption_summary or part.methods_text for part in parts
                                                if part.caption_summary or part.methods_text))
                         if any(part.caption_summary for part in parts) else None),
    )


def _report_value(value: object) -> str:
    text = str(value)
    if len(text) <= 600:
        return text
    return text[:600] + " … [abbreviated; complete value retained in the saved record]"


def setting_rows(prefix: str, value: object) -> list[tuple[str, str]]:
    """Keep nested settings readable without losing inactive or false values."""
    if isinstance(value, (list, tuple)) and len(value) > 12:
        rows = setting_rows(prefix, value[:12])
        rows.append((f"{prefix} / List summary",
                     f"{len(value)} entries; first 12 shown. Complete list retained in the saved record."))
        return rows
    if isinstance(value, Mapping) and value:
        return [row for key, child in value.items()
                for row in setting_rows(f"{prefix} / {_humanize(key)}", child)]
    if isinstance(value, (list, tuple)) and value and any(isinstance(child, Mapping) for child in value):
        return [row for index, child in enumerate(value, 1)
                for row in setting_rows(f"{prefix} {index}", child)]
    return [(prefix, _value(value))]
