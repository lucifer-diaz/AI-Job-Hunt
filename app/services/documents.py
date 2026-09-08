from __future__ import annotations

import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Protocol


class DocumentError(ValueError):
    pass


class DocumentStorage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def path(self, key: str) -> Path: ...
    def delete(self, key: str) -> None: ...


class LocalDocumentStorage:
    """Filesystem development adapter with keys confined beneath one root."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if self.root not in candidate.parents:
            raise DocumentError("Invalid storage key")
        return candidate

    def put(self, key: str, data: bytes) -> None:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)


SUPPORTED_DOCUMENTS = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}


def validate_document(filename: str, claimed_type: str | None, data: bytes) -> tuple[str, str]:
    if not filename or Path(filename).name != filename or "\\" in filename or filename in {".", ".."}:
        raise DocumentError("Unsafe filename")
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_DOCUMENTS:
        raise DocumentError("Unsupported document type; use PDF, DOCX, TXT, or MD")
    expected = SUPPORTED_DOCUMENTS[extension]
    allowed_claims = {expected, "application/octet-stream"}
    if claimed_type and claimed_type.lower().split(";", 1)[0] not in allowed_claims:
        raise DocumentError("File media type does not match its extension")
    if extension == ".pdf" and not data.startswith(b"%PDF-"):
        raise DocumentError("File content is not a PDF")
    if extension == ".docx":
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise DocumentError("File content is not a DOCX document")
        except zipfile.BadZipFile as exc:
            raise DocumentError("File content is not a DOCX document") from exc
    if extension in {".txt", ".md"}:
        if b"\x00" in data:
            raise DocumentError("Text document contains binary content")
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentError("Text documents must be UTF-8 encoded") from exc
    return extension, expected


def extract_document(data: bytes, extension: str) -> str:
    try:
        if extension == ".pdf":
            from pypdf import PdfReader

            text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)
        elif extension == ".docx":
            from docx import Document

            document = Document(BytesIO(data))
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        else:
            text = data.decode("utf-8-sig")
    except Exception as exc:
        raise DocumentError(f"Could not extract document text: {exc}") from exc
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise DocumentError("The document did not contain extractable text")
    return text
