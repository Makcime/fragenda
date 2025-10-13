from __future__ import annotations

import inspect
import math
import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML


def _ensure_pydyf_compat() -> None:
    """Patch pydyf.PDF constructor when running against legacy signatures."""
    import pydyf  # type: ignore

    init_params = inspect.signature(pydyf.PDF.__init__).parameters
    if len(init_params) == 1:
        original_init = pydyf.PDF.__init__

        def patched_init(self, version="1.7", identifier=None):
            original_init(self)
            ver = version or "1.7"
            if isinstance(ver, str):
                ver_bytes = ver.encode("ascii", "ignore")
            elif isinstance(ver, bytes):
                ver_bytes = ver
            else:
                ver_bytes = str(ver).encode("ascii", "ignore")
            self.version = ver_bytes
            self.identifier = identifier

        pydyf.PDF.__init__ = patched_init  # type: ignore[assignment]


_ensure_pydyf_compat()


PLAGE_NORMALIZATION = {"P00": "P0", "P01": "P1"}
SLOT_ORDER = ["P0", "P1", "P2", "P25", "P3", "P4", "P5"]


def _clean_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    return text


def _unique_ordered(values: Iterable[str]) -> List[str]:
    seen: List[str] = []
    for raw in values:
        text = _clean_value(raw)
        if not text:
            continue
        if text not in seen:
            seen.append(text)
    return seen


def _prettify_teacher(value: str) -> str:
    if not value:
        return ""
    text = value.replace(".", " ").replace("_", " ").strip()
    if not text:
        return ""
    parts = [part for part in text.split() if part]
    pretty = " ".join(part.capitalize() for part in parts)
    return pretty or value


def make_build_str(semver: str, stamp: datetime | None = None) -> str:
    now = stamp or datetime.now()
    return f"{semver}-{now.strftime('%Y-%m-%d-%H-%M')}"


def normalize_plage(value: str) -> str:
    v = str(value).strip().upper()
    return PLAGE_NORMALIZATION.get(v, v)


def normalize_code_to_class(code: str) -> str:
    s = str(code).strip()
    import re

    m = re.fullmatch(r"(\d+)\s*GT\s*([A-Za-z])", s, flags=re.IGNORECASE)
    if m:
        year, letter = m.group(1), m.group(2).upper()
        return f"{year}{letter}"
    return s


def _safe_week(value: str) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"A", "B"}:
        return raw
    if raw in {"AB", "A/B", "A-B", "ALL"} or raw == "":
        return "AB"
    return raw or "AB"


def _format_time(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return str(value)


@dataclass(frozen=True)
class SlotEntry:
    week: str
    course: str
    teachers: Sequence[str]
    classes: Sequence[str]
    room: str
    codes: Sequence[str]

    @property
    def is_shared(self) -> bool:
        return self.week == "AB"

    @property
    def teacher_display(self) -> str:
        return ", ".join(self.teachers)

    @property
    def class_display(self) -> str:
        return ", ".join(self.classes)

    @property
    def code_display(self) -> str:
        return " ; ".join(self.codes)

    @property
    def has_codes(self) -> bool:
        return bool(self.codes)

    @property
    def room_display(self) -> str:
        return self.room or "—"

    @property
    def has_teachers(self) -> bool:
        return bool(self.teachers)

    @property
    def has_classes(self) -> bool:
        return bool(self.classes)

    @property
    def has_room(self) -> bool:
        return bool(self.room)


@dataclass(frozen=True)
class SlotCell:
    entries: Sequence[SlotEntry]

    @property
    def is_empty(self) -> bool:
        return len(self.entries) == 0

    @property
    def layout(self) -> str:
        if self.is_empty:
            return "empty"
        if len(self.entries) == 1:
            return "single"
        weeks = {entry.week for entry in self.entries}
        if weeks <= {"A", "B"} and len(weeks) > 1:
            return "week-split"
        if weeks == {"AB"}:
            return "single"
        return "stack"


@dataclass(frozen=True)
class SlotRow:
    label: str
    interval: str
    cells: Sequence[SlotCell]


def _sorted_slots(values: Iterable[str]) -> List[str]:
    seen = []
    order = {slot: idx for idx, slot in enumerate(SLOT_ORDER)}
    for v in values:
        slot = normalize_plage(v)
        if slot not in seen:
            seen.append(slot)
    return sorted(seen, key=lambda s: order.get(s, len(order)))


def _collect_day_labels(rows: pd.DataFrame) -> Dict[int, str]:
    labels: Dict[int, str] = {}
    for day_num, chunk in rows.groupby("#day"):
        name = chunk["day"].dropna().iloc[0] if not chunk["day"].dropna().empty else str(day_num)
        labels[int(day_num)] = name
    return labels


def _build_slot_entries(day_rows: pd.DataFrame) -> List[SlotEntry]:
    if day_rows.empty:
        return []

    week_series = day_rows["week_norm"] if "week_norm" in day_rows else day_rows["semaine"].map(_safe_week)
    course_series = (
        day_rows["course_norm"] if "course_norm" in day_rows else day_rows["cours"].map(_clean_value)
    )
    room_series = (
        day_rows["local_norm"] if "local_norm" in day_rows else day_rows["local"].map(_clean_value)
    )
    teacher_series = (
        day_rows["teacher_norm"] if "teacher_norm" in day_rows else day_rows["pro"].map(_clean_value)
    )
    class_series = (
        day_rows["classe_norm"] if "classe_norm" in day_rows else day_rows["classe"].map(_clean_value)
    )
    code_series = (
        day_rows["code_norm"] if "code_norm" in day_rows else day_rows["code horaire"].map(_clean_value)
    )

    key_order: List[tuple[str, str, str]] = []
    for week, course, room in zip(week_series.tolist(), course_series.tolist(), room_series.tolist()):
        key = (week, course, room)
        if key not in key_order:
            key_order.append(key)

    entries: List[SlotEntry] = []
    for week, course, room in key_order:
        mask = (week_series == week) & (course_series == course) & (room_series == room)
        teachers = [_prettify_teacher(t) for t in _unique_ordered(teacher_series[mask].tolist())]
        classes = _unique_ordered(class_series[mask].tolist())
        codes = _unique_ordered(code_series[mask].tolist())

        entries.append(
            SlotEntry(
                week=week or "AB",
                course=course or "",
                teachers=teachers,
                classes=classes,
                room=room or "",
                codes=codes,
            )
        )

    return entries


def _prepare_matrix(rows: pd.DataFrame) -> Dict[str, List[SlotRow]]:
    day_labels = _collect_day_labels(rows)
    day_order = sorted(day_labels.keys())
    slot_order = _sorted_slots(rows["plage"])

    matrix: List[SlotRow] = []
    for slot in slot_order:
        slot_rows = rows[rows["plage_norm"] == slot]
        start = slot_rows["start"].dropna().iloc[0] if not slot_rows["start"].dropna().empty else ""
        end = slot_rows["end"].dropna().iloc[0] if not slot_rows["end"].dropna().empty else ""
        interval = " - ".join(filter(None, (_format_time(start), _format_time(end))))

        cells: List[SlotCell] = []
        for day in day_order:
            day_rows = slot_rows[slot_rows["#day"] == day]
            entries = _build_slot_entries(day_rows)
            cells.append(SlotCell(entries=entries))

        label = slot
        matrix.append(SlotRow(label=label, interval=interval, cells=cells))

    return {"day_order": day_order, "day_labels": day_labels, "rows": matrix}


def _base_group_context(group_name: str, rows: pd.DataFrame, scope: str, semver: str) -> Dict:
    matrix = _prepare_matrix(rows)
    return {
        "title": group_name,
        "scope": scope,
        "day_keys": matrix["day_order"],
        "day_labels": [matrix["day_labels"][d] for d in matrix["day_order"]],
        "slot_rows": matrix["rows"],
        "generated_on": datetime.now(),
        "build_str": make_build_str(semver),
    }


def _sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    cleaned = df.copy()
    cleaned["plage"] = cleaned["plage"].apply(normalize_plage)
    cleaned["plage_norm"] = cleaned["plage"]
    cleaned["#day"] = cleaned["#day"].astype(int)
    return cleaned


def build_class_contexts(df: pd.DataFrame, semver: str, include_general: bool = True) -> List[Dict]:
    df = _sanitize_dataframe(df)
    df["code_main"] = df["code horaire"].astype(str).apply(normalize_code_to_class)

    contexts: List[Dict] = []
    for classe, rows in df.groupby("classe"):
        rows = rows.copy()
        codes = sorted(
            {c for c in rows["code horaire"].astype(str).unique() if normalize_code_to_class(c) != classe},
            key=str.casefold,
        )
        targets = codes or [classe]
        for code in targets:
            if code == classe:
                subset = rows[rows["code_main"] == classe]
                label = f"{classe} — classe entière"
            else:
                if include_general:
                    subset = rows[
                        (rows["code horaire"].astype(str) == code) | (rows["code_main"] == classe)
                    ]
                else:
                    subset = rows[rows["code horaire"].astype(str) == code]
                label = f"{classe} — {code}"
            if subset.empty:
                continue
            contexts.append(_base_group_context(label, subset, scope="classe", semver=semver))
    return contexts


def build_group_contexts(df: pd.DataFrame, scope: str, semver: str) -> List[Dict]:
    df = _sanitize_dataframe(df)
    contexts: List[Dict] = []
    for value, rows in df.groupby(scope):
        if pd.isna(value):
            continue
        label = str(value).strip()
        contexts.append(_base_group_context(label, rows, scope=scope, semver=semver))
    return contexts


def render_contexts_to_pdfs(
    contexts: Sequence[Dict],
    template_dir: Path,
    template_name: str,
    output_dir: Path,
) -> List[Path]:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_name)

    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_paths: List[Path] = []

    tmp_dir = output_dir / ".tmp"
    os.environ.setdefault("TMPDIR", str(tmp_dir))
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for context in contexts:
        safe_name = context["title"].replace("/", "-").replace(" ", "_")
        pdf_path = output_dir / f"{safe_name}.pdf"
        html_string = template.render(context)
        HTML(string=html_string, base_url=str(template_dir)).write_pdf(str(pdf_path))
        pdf_paths.append(pdf_path)

    return pdf_paths


def generate_class_pdfs(
    dataframe: pd.DataFrame,
    semver: str,
    template_dir: Path,
    template_name: str,
    output_dir: Path,
    include_general: bool = True,
) -> List[Path]:
    contexts = build_class_contexts(dataframe, semver=semver, include_general=include_general)
    return render_contexts_to_pdfs(contexts, template_dir, template_name, output_dir)


def generate_scope_pdfs(
    dataframe: pd.DataFrame,
    scope: str,
    semver: str,
    template_dir: Path,
    template_name: str,
    output_dir: Path,
) -> List[Path]:
    contexts = build_group_contexts(dataframe, scope=scope, semver=semver)
    return render_contexts_to_pdfs(contexts, template_dir, template_name, output_dir)
