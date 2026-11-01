"""A zip of separate files is several uploads: each file is parsed on its own, as if it was uploaded alone (a zip of
bills, of workbooks). A zip that is one document stays one upload: one file in a zip, the parts of one export (CSV or
Parquet parts with one header, a manifest beside them) and a Terraform project (its .tf files are read together)."""

from __future__ import annotations

import gzip
import hashlib
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .bigfile import MB, _member_kind, _members

PROJECT_EXT = (".tf", ".tfvars", ".hcl")           # files of one Terraform project
DATA_KINDS = ("csv", "parquet")                    # parts of one export when they share a header
OFFICE_EXT = (".xlsx", ".xlsm", ".docx", ".pptx")  # zips themselves, never bundles


class TooBig(ValueError):
    """A file in the zip is larger than one upload may be."""


@dataclass
class Part:
    name: str                                      # the file's own name (no folder)
    path: Path                                     # a copy of it, on its way to the store like an upload
    sha256: str


def separate_files(path: Path, name: str, temp_path: Callable[[str], Path], limit: int) -> list[Part] | None:
    """The zip's files, each copied to a file of its own, when they are separate documents; None when the file is not
    a zip or is one document. limit: the most bytes one file may have (an upload's limit)."""
    if name.lower().endswith(OFFICE_EXT) or not zipfile.is_zipfile(path):
        return None
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return None
    with z:
        members = _members(z)
        if len(members) < 2 or any(i.filename.lower().endswith(PROJECT_EXT) for i in members):
            return None
        kinds = {i.filename: _member_kind(z, i) for i in members}
        data = [i for i in members if kinds[i.filename] in DATA_KINDS]
        rest = [i for i in members if kinds[i.filename] not in DATA_KINDS]
        if data and all("manifest" in i.filename.lower() for i in rest) and _one_header(z, data):
            return None                            # the parts of one export
        big = next((i for i in members if i.file_size > limit), None)
        if big is not None:
            raise TooBig(f"{Path(big.filename).name} in the zip is larger than {limit // MB} MB: upload it alone.")
        parts: list[Part] = []
        try:
            for i in members:
                out = temp_path(Path(i.filename).suffix)
                h = hashlib.sha256()
                with z.open(i) as src, out.open("wb") as dst:
                    while (b := src.read(8 * MB)):
                        h.update(b)
                        dst.write(b)
                parts.append(Part(Path(i.filename).name, out, h.hexdigest()))
        except BaseException:
            for p in parts:
                p.path.unlink(missing_ok=True)
            raise
        return parts


def _one_header(z: zipfile.ZipFile, data: list[zipfile.ZipInfo]) -> bool:
    """The CSV parts all start with the same header line (Parquet parts: one export when they are all Parquet)."""
    heads = set()
    for i in data:
        with z.open(i) as m:
            raw = m.read(64 * 1024)
        if raw[:4] == b"PAR1":
            heads.add(b"parquet")
            continue
        if raw[:2] == b"\x1f\x8b":
            with z.open(i) as m, gzip.open(m) as g:
                raw = g.read(64 * 1024)
        heads.add(raw.split(b"\n", 1)[0].strip())
    return len(heads) == 1
