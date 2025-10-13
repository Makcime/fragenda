from __future__ import annotations

import base64
import inspect
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time as pytime
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape
from tqdm.auto import tqdm
from weasyprint import HTML
import websocket


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
DEFAULT_REQUIRED_SLOTS: tuple[str, ...] = ("P1", "P2", "P3", "P4")
DEFAULT_SLOT_INTERVALS: dict[str, tuple[str, str]] = {
    "P0": ("08h00", "08h45"),
    "P1": ("08h45", "10h15"),
    "P2": ("10h30", "12h00"),
    "P25": ("12h00", "12h45"),
    "P3": ("13h15", "14h45"),
    "P4": ("15h00", "16h30"),
}
DEFAULT_DAY_LABELS: dict[int, str] = {
    1: "Lundi",
    2: "Mardi",
    3: "Mercredi",
    4: "Jeudi",
    5: "Vendredi",
    6: "Samedi",
}
SOFT_HYPHEN = "\u00AD"
WORD_BREAK_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]{8,}")
WEEK_ORDER = {"A": 0, "B": 1, "AB": 2}


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


def _soft_break_text(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        word = match.group(0)
        chunks = [word[i:i + 6] for i in range(0, len(word), 6)]
        return SOFT_HYPHEN.join(chunks)

    return WORD_BREAK_RE.sub(repl, text)


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
    is_option: bool = False

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
        weeks = {entry.week for entry in self.entries if entry.week}
        if any(week in {"A", "B"} for week in weeks):
            return "week-split"
        if len(self.entries) > 1:
            return "stack"
        return "single"


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


def _collect_day_labels(rows: pd.DataFrame, ensure_days: Sequence[int] | None = None) -> Dict[int, str]:
    labels: Dict[int, str] = {}
    if ensure_days:
        for day in ensure_days:
            labels[int(day)] = DEFAULT_DAY_LABELS.get(int(day), f"Jour {int(day)}")

    for day_num, chunk in rows.groupby("#day"):
        if pd.isna(day_num):
            continue
        name_series = chunk["day"].dropna()
        if not name_series.empty:
            name = str(name_series.iloc[0]).strip()
        else:
            name = DEFAULT_DAY_LABELS.get(int(day_num), str(int(day_num)))
        labels[int(day_num)] = name or DEFAULT_DAY_LABELS.get(int(day_num), str(int(day_num)))
    return labels


def _build_slot_entries(day_rows: pd.DataFrame, *, base_class: str | None = None) -> List[SlotEntry]:
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

    normalized_base = normalize_code_to_class(base_class) if base_class else None
    entries: List[SlotEntry] = []
    for week, course, room in key_order:
        mask = (week_series == week) & (course_series == course) & (room_series == room)
        teachers = [_soft_break_text(_prettify_teacher(t)) for t in _unique_ordered(teacher_series[mask].tolist())]
        classes = _unique_ordered(class_series[mask].tolist())
        codes = _unique_ordered(code_series[mask].tolist())
        is_option = False
        if normalized_base:
            normalized_codes = {normalize_code_to_class(code) for code in codes if code}
            is_option = any(code != normalized_base for code in normalized_codes)

        classes = [_soft_break_text(cls) for cls in classes]
        codes = [_soft_break_text(code) for code in codes]
        course_text = _soft_break_text(course or "")
        room_text = _soft_break_text(room or "")

        entries.append(
            SlotEntry(
                week=week or "AB",
                course=course_text,
                teachers=teachers,
                classes=classes,
                room=room_text,
                codes=codes,
                is_option=is_option,
            )
        )

    entries.sort(key=lambda entry: (WEEK_ORDER.get(entry.week, 99), entry.course, entry.room))
    return entries


def _resolve_interval(slot: str, slot_rows: pd.DataFrame, slot_times: Dict[str, tuple[str, str]]) -> str:
    start_val = ""
    end_val = ""
    if not slot_rows.empty:
        start_series = slot_rows["start"].dropna()
        end_series = slot_rows["end"].dropna()
        if not start_series.empty:
            start_val = _clean_value(start_series.iloc[0])
        if not end_series.empty:
            end_val = _clean_value(end_series.iloc[0])
    if not start_val and not end_val:
        start_val, end_val = slot_times.get(slot, DEFAULT_SLOT_INTERVALS.get(slot, ("", "")))
    interval_parts = [part for part in (start_val, end_val) if part]
    return " - ".join(interval_parts)


def _prepare_matrix(
    rows: pd.DataFrame,
    *,
    scope: str,
    base_class: str | None = None,
    ensure_slots: Sequence[str] = DEFAULT_REQUIRED_SLOTS,
    ensure_days: Sequence[int] | None = None,
) -> Dict[str, List[SlotRow]]:
    day_labels = _collect_day_labels(rows, ensure_days)
    day_order = sorted(day_labels.keys())

    slot_times: Dict[str, tuple[str, str]] = {}
    if "plage_norm" in rows and "start" in rows and "end" in rows:
        for _, record in rows[["plage_norm", "start", "end"]].dropna(subset=["plage_norm"]).iterrows():
            slot = record["plage_norm"]
            start = _clean_value(record["start"])
            end = _clean_value(record["end"])
            if slot not in slot_times and (start or end):
                slot_times[slot] = (start, end)

    source_slots = rows["plage_norm"] if "plage_norm" in rows else rows.get("plage", pd.Series(dtype=object))
    present_slots = {normalize_plage(value) for value in source_slots.dropna()}

    ensure_set = {normalize_plage(slot) for slot in ensure_slots}
    ordered_slots: List[str] = []
    for slot in SLOT_ORDER:
        if slot in ensure_set or slot in present_slots:
            ordered_slots.append(slot)
    additional_slots = sorted(present_slots - set(SLOT_ORDER))
    ordered_slots.extend(additional_slots)

    matrix_rows: List[SlotRow] = []

    for slot in ordered_slots:
        slot_rows = rows[rows["plage_norm"] == slot] if "plage_norm" in rows else rows[rows["plage"] == slot]
        interval = _resolve_interval(slot, slot_rows, slot_times)

        cells: List[SlotCell] = []
        for day in day_order:
            if "plage_norm" in rows:
                day_rows = rows[(rows["plage_norm"] == slot) & (rows["#day"] == day)]
            else:
                day_rows = rows[(rows["plage"] == slot) & (rows["#day"] == day)]
            entries = _build_slot_entries(day_rows, base_class=base_class if scope == "classe" else None)
            cells.append(SlotCell(entries=tuple(entries)))

        matrix_rows.append(SlotRow(label=slot, interval=interval, cells=tuple(cells)))

    if scope == "pro" and day_order:
        friday_index = day_order.index(5) if 5 in day_order else None
        concertation_applied = False
        updated_rows: List[SlotRow] = []
        for row in matrix_rows:
            if row.label == "P4" and friday_index is not None:
                cells = list(row.cells)
                entries = list(cells[friday_index].entries)
                if not any((entry.course or "").strip().lower() == "concertation" for entry in entries):
                    entries.append(
                        SlotEntry(
                            week="AB",
                            course="Concertation",
                            teachers=(),
                            classes=(),
                            room="",
                            codes=(),
                            is_option=False,
                        )
                    )
                cells[friday_index] = SlotCell(entries=tuple(entries))
                updated_rows.append(SlotRow(label=row.label, interval=row.interval, cells=tuple(cells)))
                concertation_applied = True
            else:
                updated_rows.append(row)
        if not concertation_applied and friday_index is not None:
            empty_cells = [SlotCell(entries=tuple()) for _ in day_order]
            entries = [
                SlotEntry(
                    week="AB",
                    course="Concertation",
                    teachers=(),
                    classes=(),
                    room="",
                    codes=(),
                    is_option=False,
                )
            ]
            empty_cells[friday_index] = SlotCell(entries=tuple(entries))
            interval = _resolve_interval("P4", pd.DataFrame(columns=rows.columns), slot_times)
            updated_rows.append(SlotRow(label="P4", interval=interval, cells=tuple(empty_cells)))
        matrix_rows = updated_rows

    return {"day_order": day_order, "day_labels": day_labels, "rows": matrix_rows}


def _base_group_context(
    group_name: str,
    rows: pd.DataFrame,
    scope: str,
    semver: str,
    *,
    base_class: str | None = None,
) -> Dict:
    ensure_days = tuple(range(1, 6)) if scope == "pro" else None
    matrix = _prepare_matrix(
        rows,
        scope=scope,
        base_class=base_class,
        ensure_slots=DEFAULT_REQUIRED_SLOTS,
        ensure_days=ensure_days,
    )
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
                label = classe
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
            contexts.append(
                _base_group_context(label, subset, scope="classe", semver=semver, base_class=classe)
            )
    return contexts


def build_group_contexts(df: pd.DataFrame, scope: str, semver: str) -> List[Dict]:
    df = _sanitize_dataframe(df)
    contexts: List[Dict] = []
    for value, rows in df.groupby(scope):
        if pd.isna(value):
            continue
        label = str(value).strip()
        if scope == "pro":
            label = _prettify_teacher(label)
        contexts.append(_base_group_context(label, rows, scope=scope, semver=semver))
    return contexts


VALID_RENDERERS = {"weasyprint", "wkhtmltopdf", "chromium"}


def _chromium_print_pdf(chromium_cmd: str, html_path: Path, pdf_path: Path, timeout: float = 45.0) -> None:
    user_data = tempfile.mkdtemp(prefix="chromium-print-")
    proc = None
    try:
        cmd = [
            chromium_cmd,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            f"--user-data-dir={user_data}",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        ws_url = None
        start = pytime.time()
        assert proc.stderr is not None
        while pytime.time() - start < timeout:
            line = proc.stderr.readline()
            if not line:
                if proc.poll() is not None:
                    break
                pytime.sleep(0.05)
                continue
            if "DevTools listening on" in line:
                ws_url = line.split("DevTools listening on", 1)[-1].strip()
                break

        if not ws_url:
            raise RuntimeError("Chromium did not expose a DevTools endpoint")

        parsed = urllib.parse.urlparse(ws_url)
        version_url = f"http://{parsed.netloc}/json/version"
        version_info = json.load(urllib.request.urlopen(version_url))
        browser_ws = websocket.create_connection(version_info["webSocketDebuggerUrl"], timeout=timeout)
        base_http = f"http://{parsed.netloc}"
        html_uri = html_path.resolve().as_uri()

        try:
            message_id = 0

            def send(method: str, params: Dict | None = None, session: str | None = None) -> int:
                nonlocal message_id
                message_id += 1
                payload = {
                    "id": message_id,
                    "method": method,
                    "params": params or {},
                }
                if session:
                    payload["sessionId"] = session
                browser_ws.send(json.dumps(payload))
                return message_id

            def recv() -> Dict:
                return json.loads(browser_ws.recv())

            def wait_for_response(command_id: int) -> Dict:
                while True:
                    resp = recv()
                    if resp.get("id") == command_id:
                        return resp

            create_id = send("Target.createTarget", {"url": "about:blank"})
            create_result = wait_for_response(create_id)["result"]
            target_id = create_result["targetId"]

            attach_id = send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            attach_result = wait_for_response(attach_id)["result"]
            session_id = attach_result["sessionId"]

            send("Target.activateTarget", {"targetId": target_id})
            send("Page.enable", session=session_id)

            nav_id = send("Page.navigate", {"url": html_uri}, session=session_id)

            loaded = False
            while not loaded:
                resp = recv()
                if resp.get("sessionId") == session_id:
                    if resp.get("method") in {"Page.loadEventFired", "Page.frameStoppedLoading"}:
                        loaded = True
                    elif resp.get("id") == nav_id and "error" in resp:
                        raise RuntimeError(f"Chromium navigation failed: {resp['error']}")

            pdf_id = send(
                "Page.printToPDF",
                {
                    "printBackground": True,
                    "displayHeaderFooter": False,
                    "landscape": True,
                    "paperWidth": 11.69,
                    "paperHeight": 8.27,
                    "marginTop": 0.4,
                    "marginBottom": 0.4,
                    "marginLeft": 0.5,
                    "marginRight": 0.5,
                },
                session=session_id,
            )

            pdf_data = None
            while pdf_data is None:
                resp = recv()
                if resp.get("id") == pdf_id:
                    if "error" in resp:
                        raise RuntimeError(f"Chromium print failed: {resp['error']}")
                    pdf_data = resp["result"]["data"]

            pdf_path.write_bytes(base64.b64decode(pdf_data))

            send("Target.closeTarget", {"targetId": target_id})

        finally:
            try:
                browser_ws.close()
            except Exception:
                pass
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(user_data, ignore_errors=True)


def render_contexts_to_pdfs(
    contexts: Sequence[Dict],
    template_dir: Path,
    template_name: str,
    output_dir: Path,
    *,
    progress_desc: str | None = None,
    export_html: bool = True,
    renderer: str = "weasyprint",
) -> List[Path]:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_name)

    renderer = (renderer or "weasyprint").lower()
    if renderer not in VALID_RENDERERS:
        raise ValueError(f"Unsupported renderer '{renderer}'")

    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_paths: List[Path] = []

    tmp_dir = output_dir / ".tmp"
    os.environ.setdefault("TMPDIR", str(tmp_dir))
    tmp_dir.mkdir(parents=True, exist_ok=True)

    context_list = list(contexts)
    iterator = (
        tqdm(
            context_list,
            desc=progress_desc or "Rendering",
            unit="doc",
        )
        if context_list
        else context_list
    )

    logo_path = (template_dir.parent / "Logo Ecole .jpg").resolve()
    logo_data_uri = ""
    if logo_path.exists():
        mime = "image/jpeg"
        if logo_path.suffix.lower() in {".png"}:
            mime = "image/png"
        elif logo_path.suffix.lower() in {".gif"}:
            mime = "image/gif"
        encoded = base64.b64encode(logo_path.read_bytes()).decode("ascii")
        logo_data_uri = f"data:{mime};base64,{encoded}"

    need_html_files = export_html or renderer in {"wkhtmltopdf", "chromium"}
    html_dir = output_dir / "html" if need_html_files else None
    if html_dir:
        html_dir.mkdir(parents=True, exist_ok=True)

    wkhtml_command = None
    if renderer == "wkhtmltopdf":
        wkhtml_command = shutil.which("wkhtmltopdf")
        if not wkhtml_command:
            raise RuntimeError("wkhtmltopdf binary not found. Install it or switch renderer back to weasyprint.")

    chromium_command = None
    if renderer == "chromium":
        for candidate in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "chrome",
            "chromium-headless-shell",
        ):
            path = shutil.which(candidate)
            if path:
                chromium_command = path
                break
        if not chromium_command:
            raise RuntimeError(
                "Chromium/Chrome executable not found. Install Google Chrome or Chromium and retry, "
                "or choose a different renderer."
            )

    for context in iterator:
        render_context = dict(context)
        render_context.setdefault("logo_src", logo_data_uri)
        safe_name = context["title"].replace("/", "-").replace(" ", "_")
        pdf_path = output_dir / f"{safe_name}.pdf"
        html_string = template.render(render_context)

        html_path: Path | None = None
        if html_dir:
            html_path = html_dir / f"{safe_name}.html"
            html_path.write_text(html_string, encoding="utf-8")

        if renderer == "weasyprint":
            HTML(string=html_string, base_url=str(template_dir)).write_pdf(str(pdf_path))
        elif renderer == "wkhtmltopdf":
            if not html_path:
                html_path = tmp_dir / f"{safe_name}.html"
                html_path.write_text(html_string, encoding="utf-8")
            cmd = [
                wkhtml_command,
                "--encoding",
                "utf-8",
                "--orientation",
                "Landscape",
                "--margin-top",
                "10mm",
                "--margin-bottom",
                "10mm",
                "--margin-left",
                "12mm",
                "--margin-right",
                "12mm",
                "--viewport-size",
                "1600x1000",
                "--dpi",
                "150",
                "--zoom",
                "1.35",
                str(html_path),
                str(pdf_path),
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:  # chromium
            if not html_path:
                html_path = tmp_dir / f"{safe_name}.html"
                html_path.write_text(html_string, encoding="utf-8")
            _chromium_print_pdf(chromium_command, html_path, pdf_path)

        pdf_paths.append(pdf_path)

    return pdf_paths


def generate_class_pdfs(
    dataframe: pd.DataFrame,
    semver: str,
    template_dir: Path,
    template_name: str,
    output_dir: Path,
    include_general: bool = True,
    renderer: str = "weasyprint",
) -> List[Path]:
    contexts = build_class_contexts(dataframe, semver=semver, include_general=include_general)
    return render_contexts_to_pdfs(
        contexts,
        template_dir,
        template_name,
        output_dir,
        progress_desc="Classes",
        renderer=renderer,
    )


def generate_scope_pdfs(
    dataframe: pd.DataFrame,
    scope: str,
    semver: str,
    template_dir: Path,
    template_name: str,
    output_dir: Path,
    renderer: str = "weasyprint",
) -> List[Path]:
    contexts = build_group_contexts(dataframe, scope=scope, semver=semver)
    desc_lookup = {
        "classe": "Classes",
        "pro": "Pros",
        "local": "Locaux",
    }
    return render_contexts_to_pdfs(
        contexts,
        template_dir,
        template_name,
        output_dir,
        progress_desc=desc_lookup.get(scope, scope.capitalize()),
        renderer=renderer,
    )
