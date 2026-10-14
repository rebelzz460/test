#!/usr/bin/env python3
"""Portable mistake-workbook renderer. Layout evidence is not subject validation.

Dependencies: ReportLab, Pillow, pypdf (checker). See ../references/workbook-data.md.
The renderer prints only schema-approved teaching fields, never an audit ledger.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Image, Paragraph, Spacer, Table, TableStyle, XPreformatted
from reportlab.platypus import paragraph as paragraph_module

# ReportLab's CJK line-start exclusions omit several fullwidth Chinese marks.
# Extend the existing prohibition rather than allowing a lone closing mark line.
paragraph_module.ALL_CANNOT_START += "，。！？；：…）》」』】〕〉〗〙〛"

VERSION = "1.0"
ALLOWED_TAG = re.compile(r"(</?(?:b|i|super|sub)>|<br\s*/?>)", re.I)
AUDIT_KEYS = {"photo_path", "source_image", "source_image_path", "image_path",
              "source_coordinates", "crop_coordinates", "crop_region", "ocr_confidence",
              "transcription_confidence", "source_bank_qid", "evidence", "audit", "ledger",
              "production_notes", "verification_notes", "source_photo", "source_file"}
AUDIT_KEYS.update({"internal_audit", "source_notes", "photo_region", "print_notes",
                   "layout_notes", "production_statement", "usage_instructions", "version_note"})
TEXT_LABELS = {
    "zh": {"toc": "目录", "original": "原题", "knowledge": "本题考查的知识",
           "keywords": "知识关键词", "diagnostic": "诊断小问", "diag_answers": "诊断小问参考答案",
           "methods": "解题思路", "class_diagnosis": "思维诊断", "solutions": "原题解析",
           "answer": "参考答案", "alternative": "其他解题角度", "diagnosis": "易错思维与判断",
           "practice": "新题补练", "practice_item": "新题", "practice_answers": "新题答案与解析"},
    "en": {"toc": "Contents", "original": "Original question", "knowledge": "Knowledge examined",
           "keywords": "Keywords", "diagnostic": "Diagnostic question", "diag_answers": "Diagnostic answers",
           "methods": "Methods", "class_diagnosis": "Thinking diagnosis", "solutions": "Original solutions",
           "answer": "Reference answer", "alternative": "Other approaches", "diagnosis": "Pitfalls and diagnosis",
           "practice": "New practice", "practice_item": "Practice question", "practice_answers": "Practice answers and solutions"},
}


class WorkbookError(ValueError):
    pass


def plain_text(text: str) -> str:
    """Remove only the explicitly supported formatting; comparisons stay literal."""
    return ALLOWED_TAG.sub(lambda m: "\n" if m.group(0).lower().startswith("<br") else "", text)


def normalized(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def require_string(value: Any, path: str, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise WorkbookError(f"{path}: expected {'a string' if allow_empty else 'a non-empty string'}")


def validate_blocks(blocks: Any, path: str, base: Path, nonempty: bool = False) -> None:
    if not isinstance(blocks, list) or (nonempty and not blocks):
        raise WorkbookError(f"{path}: expected {'a non-empty' if nonempty else 'a'} block list")
    for i, block in enumerate(blocks):
        bp = f"{path}[{i}]"
        if not isinstance(block, dict):
            raise WorkbookError(f"{bp}: expected an object")
        kind = block.get("type")
        if kind in ("text", "code"):
            require_string(block.get("text"), bp + ".text")
        elif kind == "image":
            require_string(block.get("path"), bp + ".path")
            p = (base / block["path"]).resolve()
            if not p.is_file():
                raise WorkbookError(f"{bp}: image not found: {p}")
            for key in ("width_pt", "height_pt"):
                if key in block and (not isinstance(block[key], (int, float)) or block[key] <= 0):
                    raise WorkbookError(f"{bp}.{key}: expected a positive number")
            if "caption" in block:
                require_string(block["caption"], bp + ".caption", True)
        elif kind == "table":
            rows = block.get("rows")
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], list) or not rows[0]:
                raise WorkbookError(f"{bp}.rows: expected non-empty rectangular rows")
            n = len(rows[0])
            for r in rows:
                if not isinstance(r, list) or len(r) != n or any(not isinstance(c, str) for c in r):
                    raise WorkbookError(f"{bp}.rows: all cells must be strings in rectangular rows")
            headers = block.get("header_rows", 0)
            if not isinstance(headers, int) or not 0 <= headers < len(rows):
                raise WorkbookError(f"{bp}.header_rows: must be between zero and rows - 1")
            weights = block.get("widths", [1] * n)
            if len(weights) != n or any(not isinstance(w, (int, float)) or w <= 0 for w in weights):
                raise WorkbookError(f"{bp}.widths: expected one positive weight per column")
        else:
            raise WorkbookError(f"{bp}.type: unsupported {kind!r}; use text, image, table or code")


def validate_data(data: Any, base: Path, practice_count: int = 3) -> dict:
    if not isinstance(data, dict):
        raise WorkbookError("Root must be a JSON object")
    require_string(data.get("title"), "title")
    classes = data.get("classes")
    if not isinstance(classes, list) or not classes:
        raise WorkbookError("classes: expected a non-empty list")
    if practice_count < 1:
        raise WorkbookError("practice-count must be at least one")
    ids: set[str] = set()
    counts = {"classes": len(classes), "originals": 0, "practice": 0}

    def unique(obj: dict, path: str) -> None:
        require_string(obj.get("id"), path + ".id")
        if obj["id"] in ids:
            raise WorkbookError(f"{path}.id: duplicate ID {obj['id']!r}")
        ids.add(obj["id"])

    for ci, cls in enumerate(classes):
        cp = f"classes[{ci}]"
        if not isinstance(cls, dict):
            raise WorkbookError(f"{cp}: expected an object")
        unique(cls, cp)
        require_string(cls.get("title"), cp + ".title")
        if "scope" in cls:
            require_string(cls["scope"], cp + ".scope", True)
        if not isinstance(cls.get("originals"), list) or not cls["originals"]:
            raise WorkbookError(f"{cp}.originals: expected a non-empty list")
        if not isinstance(cls.get("methods"), list) or not cls["methods"]:
            raise WorkbookError(f"{cp}.methods: expected a non-empty list")
        for mi, method in enumerate(cls["methods"]):
            mp = f"{cp}.methods[{mi}]"
            require_string(method.get("title"), mp + ".title")
            validate_blocks(method.get("content"), mp + ".content", base, True)
        validate_blocks(cls.get("diagnosis", []), cp + ".diagnosis", base)
        practice = cls.get("practice")
        if not isinstance(practice, list) or len(practice) != practice_count:
            raise WorkbookError(f"{cp}.practice: expected exactly {practice_count} questions; change --practice-count only when requested")
        for kind in ("originals", "practice"):
            for qi, q in enumerate(cls[kind]):
                qp = f"{cp}.{kind}[{qi}]"
                if not isinstance(q, dict):
                    raise WorkbookError(f"{qp}: expected an object")
                unique(q, qp)
                counts[kind] += 1
                validate_blocks(q.get("question"), qp + ".question", base, True)
                opts = q.get("options", [])
                if not isinstance(opts, list):
                    raise WorkbookError(f"{qp}.options: expected a list, including [] for open questions")
                labels = []
                for oi, opt in enumerate(opts):
                    op = f"{qp}.options[{oi}]"
                    if not isinstance(opt, dict):
                        raise WorkbookError(f"{op}: expected an object")
                    require_string(opt.get("label"), op + ".label")
                    require_string(opt.get("text"), op + ".text")
                    labels.append(opt["label"])
                if len(set(labels)) != len(labels):
                    raise WorkbookError(f"{qp}.options: labels must be unique and retain source order")
                if "correct_labels" in q:
                    selected = q["correct_labels"]
                    if not isinstance(selected, list) or not selected or len(selected) != len(set(selected)) or any(s not in labels for s in selected):
                        raise WorkbookError(f"{qp}.correct_labels: selected labels must be unique existing option labels")
                require_string(q.get("answer"), qp + ".answer")
                knowledge = q.get("knowledge")
                if not isinstance(knowledge, list) or not knowledge:
                    raise WorkbookError(f"{qp}.knowledge: expected detailed knowledge statements")
                for ki, text in enumerate(knowledge):
                    require_string(text, f"{qp}.knowledge[{ki}]")
                validate_blocks(q.get("solution"), qp + ".solution", base, True)
                for key in ("alternatives", "diagnosis"):
                    validate_blocks(q.get(key, []), qp + "." + key, base)
                if kind == "originals":
                    keywords = q.get("keywords")
                    if not isinstance(keywords, list) or not keywords:
                        raise WorkbookError(f"{qp}.keywords: expected a non-empty string list")
                    for ki, text in enumerate(keywords):
                        require_string(text, f"{qp}.keywords[{ki}]")
                    validate_blocks(q.get("diagnostic"), qp + ".diagnostic", base, True)
                    validate_blocks(q.get("diagnostic_answer"), qp + ".diagnostic_answer", base, True)
                    if q.get("source_label"):
                        require_string(q["source_label"], qp + ".source_label")
                        if re.search(r"(?:[A-Za-z]:[\\/]|\.(?:jpe?g|png|tiff?|heic)\b|(?:crop|照片|裁图|坐标)\s*[:：])", q["source_label"], re.I):
                            raise WorkbookError(f"{qp}.source_label: photo paths/coordinates belong in the separate audit ledger")
    return counts


class Fonts:
    """Check each visible glyph and explicitly select an available embedded font."""
    def __init__(self, base: Path, configured: dict | None = None):
        configured = configured or {}
        win = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        choices = {
            "body": [win / "simsun.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", "/Library/Fonts/Arial Unicode.ttf"],
            "heading": [win / "simhei.ttf", win / "arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
            "latin": [win / "times.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", win / "arial.ttf"],
            "fallback": [win / "ARIALUNI.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
            "mono": [win / "consola.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"],
            "italic": [win / "timesi.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf", win / "ariali.ttf"],
        }
        self.names, self.paths, self.coverage, self.used = {}, {}, {}, set()
        for role, candidates in choices.items():
            explicit = configured.get(role)
            if explicit:
                candidate = (base / explicit).resolve()
                if not candidate.is_file():
                    raise WorkbookError(f"fonts.{role}: font not found: {candidate}")
            else:
                candidate = next((Path(p) for p in candidates if Path(p).is_file()), None)
            if candidate is None:
                continue
            name = "WB_" + hashlib.sha256(str(candidate).encode()).hexdigest()[:12]
            try:
                if name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(name, str(candidate)))
                face = pdfmetrics.getFont(name).face
                coverage = {cp for cp, glyph in face.charToGlyph.items() if glyph != 0}
            except Exception as exc:
                if explicit:
                    raise WorkbookError(f"fonts.{role}: cannot embed {candidate}: {exc}; select a static TrueType font or native renderer") from exc
                continue
            self.names[role], self.paths[role], self.coverage[name] = name, str(candidate), coverage
        if "body" not in self.names:
            raise WorkbookError("No embeddable body font discovered. Configure fonts.body with a TrueType path")
        for role in choices:
            self.names.setdefault(role, self.names["body"])
        for name in set(self.names.values()):
            pdfmetrics.registerFontFamily(name, normal=name, bold=name, italic=name, boldItalic=name)

    def choose(self, char: str, role: str, path: str) -> str:
        cp = ord(char)
        if char in "\n\r\t":
            return self.names[role]
        # ReportLab's unshaped text is unsuitable for these writing systems.
        if (0x0590 <= cp <= 0x0FFF or 0x1780 <= cp <= 0x17FF) and not (0x0900 > cp > 0x08FF):
            raise WorkbookError(f"{path}: complex-script character U+{cp:04X}; use a shaping-capable native renderer")
        priority = [role]
        if role == "body" and (cp < 0x0250 or 0x2018 <= cp <= 0x201D):
            priority.insert(0, "latin")
        priority += ["fallback", "body", "latin", "heading", "mono"]
        for r in priority:
            name = self.names[r]
            if cp in self.coverage[name]:
                self.used.add(name)
                return name
        raise WorkbookError(f"{path}: no configured font covers {char!r} (U+{cp:04X}); supply fonts.fallback or use another native renderer")

    def markup(self, text: str, role: str = "body", path: str = "text", literal: bool = False) -> str:
        parts = [text] if literal else ALLOWED_TAG.split(text)
        result, bold, italic = [], False, False
        for part in parts:
            if not part:
                continue
            if not literal and ALLOWED_TAG.fullmatch(part):
                tag = part.lower()
                if tag == "<b>": bold = True
                elif tag == "</b>": bold = False
                elif tag == "<i>": italic = True
                elif tag == "</i>": italic = False
                else: result.append("<br/>" if tag.startswith("<br") else tag)
                continue
            selected_role = "heading" if bold else "italic" if italic else role
            run, runfont = [], None
            for char in part:
                if char == "\n" and not literal:
                    if run:
                        result.append(f'<font name="{runfont}">{html.escape("".join(run))}</font>')
                        run = []
                    result.append("<br/>")
                    runfont = None
                    continue
                name = self.choose(char, selected_role, path)
                if runfont != name and run:
                    result.append(f'<font name="{runfont}">{html.escape("".join(run))}</font>')
                    run = []
                runfont = name
                run.append(char)
            if run:
                result.append(f'<font name="{runfont}">{html.escape("".join(run))}</font>')
        return "".join(result)

    def width(self, text: str, size: float, role: str = "body", path: str = "text") -> float:
        return sum(pdfmetrics.stringWidth(c, self.choose(c, role, path), size) for c in plain_text(text) if c not in "\n\r")


@dataclass
class Unit:
    flow: Any
    path: str | None = None
    text: str | None = None
    kind: str = "text"
    keep_next: bool = False
    extra: dict = field(default_factory=dict)


@dataclass
class Placed:
    unit: Unit
    top: float
    height: float


@dataclass
class Page:
    pieces: list[Placed] = field(default_factory=list)
    used: float = 0


class Planner:
    def __init__(self, width: float, height: float):
        self.width, self.height = width, height
        self.pages = [Page()]

    @property
    def page(self) -> Page:
        return self.pages[-1]

    @property
    def remaining(self) -> float:
        return self.height - self.page.used

    def new_page(self) -> None:
        if self.page.pieces:
            self.pages.append(Page())

    def size(self, unit: Unit) -> float:
        return unit.flow.wrap(self.width, self.height)[1]

    def total(self, units: list[Unit]) -> float:
        return sum(self.size(u) for u in units)

    def put(self, unit: Unit, height: float) -> None:
        if height > self.remaining + 0.02:
            raise WorkbookError(f"Internal overflow at {unit.path}")
        self.page.pieces.append(Placed(unit, self.page.used, height))
        self.page.used += height

    def group(self, units: list[Unit]) -> None:
        height = self.total(units)
        if height > self.height + 0.02:
            raise WorkbookError("An inseparable question tail / diagnostic group exceeds one page; shorten the diagnostic or select a suitable native renderer")
        if height > self.remaining + 0.02:
            self.new_page()
        for unit in units:
            self.put(unit, self.size(unit))

    def add(self, units: list[Unit]) -> None:
        todo = list(units)
        while todo:
            unit = todo.pop(0)
            h = self.size(unit)
            if unit.keep_next and todo:
                # Keep headings with at least two lines of the next content.
                need = h
                for following in todo:
                    if following.kind == "spacing" or following.keep_next:
                        need += self.size(following)
                        continue
                    style = getattr(following.flow, "style", None)
                    need += min(self.size(following), max(26, style.leading * 2 if style else 26))
                    break
                if need > self.remaining + 0.02 and self.page.pieces:
                    self.new_page()
            if h <= self.remaining + 0.02:
                self.put(unit, h)
                continue
            try:
                pieces = unit.flow.split(self.width, self.remaining)
            except Exception as exc:
                raise WorkbookError(f"{unit.path}: cannot split content: {exc}") from exc
            if pieces:
                first_h = pieces[0].wrap(self.width, self.height)[1]
                if first_h <= self.remaining + 0.02 and first_h > 0:
                    first = copy.copy(unit); first.flow = pieces[0]; first.keep_next = False
                    self.put(first, first_h)
                    rest = []
                    for f in pieces[1:]:
                        next_unit = copy.copy(unit); next_unit.flow = f; next_unit.keep_next = False
                        rest.append(next_unit)
                    todo = rest + todo
                    self.new_page()
                    continue
            if not self.page.pieces:
                raise WorkbookError(f"{unit.path}: an unsplittable block is taller than the usable page; choose a native renderer or revise its explicit size")
            self.new_page()
            todo.insert(0, unit)


class Builder:
    def __init__(self, data: dict, base: Path, args: argparse.Namespace):
        self.data, self.base, self.args = data, base, args
        self.labels = TEXT_LABELS["zh" if data.get("language", "zh").startswith("zh") else "en"]
        layout = data.get("layout", {})
        self.body = args.body_pt if args.body_pt is not None else layout.get("body_pt", 9.5)
        self.leading = args.leading_pt if args.leading_pt is not None else layout.get("leading_pt", 12.6)
        if not isinstance(self.body, (int, float)) or self.body < 9:
            raise WorkbookError("Body size must be at least 9 pt; do not shrink content to make it fit")
        if not isinstance(self.leading, (int, float)) or self.leading < self.body * 1.15:
            raise WorkbookError("leading_pt must be at least 1.15 times body_pt")
        self.margin = args.margin_mm * 72 / 25.4
        if self.margin < 10 * 72 / 25.4 or self.margin > 35 * 72 / 25.4:
            raise WorkbookError("margin-mm must be between 10 and 35 for an A4 workbook")
        self.width = A4[0] - 2 * self.margin
        self.height = A4[1] - 2 * self.margin - 26
        configured = dict(data.get("fonts", {}))
        for role in ("body", "heading", "latin", "fallback", "mono"):
            value = getattr(args, "font_" + role, None)
            if value: configured[role] = str(Path(value).resolve())
        self.fonts = Fonts(base, configured)
        self.styles = {
            "body": ParagraphStyle("WBBody", fontName=self.fonts.names["body"], fontSize=self.body,
                leading=self.leading, textColor=colors.black, alignment=TA_LEFT,
                wordWrap="CJK", allowWidows=0, allowOrphans=0, splitLongWords=1),
            "heading": ParagraphStyle("WBHeading", fontName=self.fonts.names["heading"], fontSize=self.body + 2,
                leading=self.leading + 3, textColor=colors.black, wordWrap="CJK", allowWidows=0, allowOrphans=0),
            "small_heading": ParagraphStyle("WBSubheading", fontName=self.fonts.names["heading"], fontSize=self.body,
                leading=self.leading, textColor=colors.black, wordWrap="CJK", allowWidows=0, allowOrphans=0),
            "title": ParagraphStyle("WBTitle", fontName=self.fonts.names["heading"], fontSize=self.body + 6,
                leading=self.leading + 8, textColor=colors.black, wordWrap="CJK"),
            "code": ParagraphStyle("WBCode", fontName=self.fonts.names["mono"], fontSize=self.body,
                leading=self.leading, textColor=colors.black, allowWidows=0, allowOrphans=0),
        }
        self.plan = Planner(self.width, self.height)
        self.classes, self.originals, self.practice, self.images = [], [], [], []
        self.blocks: dict[str, dict] = {}
        self.original_index = self.practice_index = 0

    def paragraph(self, text: str, path: str | None, style: str = "body", keep: bool = False, role: str | None = None) -> Unit:
        role = role or ("heading" if style != "body" else "body")
        markup = self.fonts.markup(text, role, path or "heading")
        flow = Paragraph(markup, self.styles[style])
        return Unit(flow, path, plain_text(text), "text", keep)

    def gap(self, points: float = 4) -> Unit:
        return Unit(Spacer(1, points), kind="spacing")

    def heading(self, text: str, path: str | None = None, large: bool = False) -> list[Unit]:
        tail_gap = self.gap(2); tail_gap.keep_next = True
        return [self.gap(5), self.paragraph(text, path, "heading" if large else "small_heading", True), tail_gap]

    def blocks_from(self, blocks: list[dict], path: str) -> list[Unit]:
        units = []
        for bi, block in enumerate(blocks):
            bp = f"{path}[{bi}]"
            kind = block["type"]
            if kind == "text":
                units.append(self.paragraph(block["text"], bp + ".text"))
            elif kind == "code":
                # Preserve original whitespace. Soft wrap long lines without deleting characters.
                wrapped = []
                for line in block["text"].replace("\t", "    ").split("\n"):
                    current, width = "", 0.0
                    for char in line:
                        w = self.fonts.width(char, self.body, "mono", bp)
                        if current and width + w > self.width - 6:
                            wrapped.append(current); current, width = "", 0
                        current += char; width += w
                    wrapped.append(current)
                markup = self.fonts.markup("\n".join(wrapped), "mono", bp, literal=True)
                units.append(Unit(XPreformatted(markup, self.styles["code"]), bp + ".text", block["text"], "code"))
            elif kind == "image":
                p = (self.base / block["path"]).resolve()
                try:
                    im = PILImage.open(p)
                    im.load()
                    # Transparency becomes white; colours become grayscale in the PDF.
                    if "A" in im.getbands() or im.mode == "P":
                        rgba = im.convert("RGBA")
                        background = PILImage.new("RGBA", rgba.size, "white")
                        background.alpha_composite(rgba)
                        im = background.convert("L")
                    else:
                        im = im.convert("L")
                except Exception as exc:
                    raise WorkbookError(f"{bp}: cannot read image {p}: {exc}; use a supported raster or native renderer") from exc
                w = block.get("width_pt", min(self.width, im.width * 72 / 150))
                h = block.get("height_pt", w * im.height / im.width)
                if "width_pt" in block and "height_pt" in block:
                    # Explicit dimensions define a containing box, not a request
                    # to distort a diagram's angles or an equation's glyphs.
                    fit = min(w / im.width, h / im.height)
                    w, h = im.width * fit, im.height * fit
                elif "height_pt" in block:
                    w = h * im.width / im.height
                scale = min(1.0, self.width / w)
                w, h = w * scale, h * scale
                if h > self.height - 2 * self.leading:
                    raise WorkbookError(f"{bp}: image is too tall at the requested readable size; split it meaningfully or use a native renderer")
                out = io.BytesIO(); im.save(out, "PNG"); out.seek(0)
                units.append(Unit(Image(out, width=w, height=h, hAlign="LEFT"), bp, None, "image", bool(block.get("caption")),
                                  {"pixel_width": im.width, "pixel_height": im.height, "draw_width_pt": w, "draw_height_pt": h}))
                self.images.append({"path": bp, "file": str(p), "effective_dpi": round(min(im.width * 72 / w, im.height * 72 / h), 1)})
                if block.get("caption"):
                    units.append(self.paragraph(block["caption"], bp + ".caption"))
            elif kind == "table":
                rows = block["rows"]
                weights = block.get("widths", [1] * len(rows[0]))
                widths = [self.width * w / sum(weights) for w in weights]
                if min(widths) < self.body * 2:
                    raise WorkbookError(f"{bp}: table columns are too narrow at readable font size; choose a native renderer or restructure the table")
                headers = block.get("header_rows", 0)
                cells = [[self.paragraph(c, None, "small_heading" if ri < headers else "body").flow for c in r] for ri, r in enumerate(rows)]
                table = Table(cells, colWidths=widths, repeatRows=headers, splitByRow=1, splitInRow=1,
                              style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                                  ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                                  ("GRID", (0, 0), (-1, -1), 0.35, colors.black)]))
                units.append(Unit(table, bp, "\n".join(" ".join(plain_text(c) for c in r) for r in rows), "table",
                                  extra={"cells": [[plain_text(c) for c in r] for r in rows]}))
            units.append(self.gap(2))
        return units

    def option_units(self, options: list[dict], path: str) -> list[Unit]:
        if not options:
            return []
        units, i = [], 0
        while i < len(options):
            # Consecutive short choices occupy a row; row-major order is never changed.
            end, used = i, 0
            while end < len(options):
                text = options[end]["label"] + ". " + options[end]["text"]
                w = self.fonts.width(text, self.body, path=f"{path}[{end}]") + 16
                if "\n" in text or w > self.width or (end > i and used + w > self.width):
                    break
                used += w; end += 1
            if end - i < 2:
                opt = options[i]
                units.append(self.paragraph(opt["label"] + ". " + opt["text"], f"{path}[{i}]"))
                i += 1
                continue
            subset = options[i:end]
            texts = [o["label"] + ". " + o["text"] for o in subset]
            widths = [self.fonts.width(t, self.body) + 16 for t in texts]
            widths[-1] += self.width - sum(widths)
            flows = [self.paragraph(t, None).flow for t in texts]
            table = Table([flows], colWidths=widths, style=TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
            units.append(Unit(table, path + f"[row:{i}:{end}]", " ".join(plain_text(t) for t in texts), "options",
                              extra={"option_paths": [f"{path}[{j}]" for j in range(i, end)], "option_texts": texts}))
            i = end
        return units + [self.gap(2)]

    def question_units(self, q: dict, path: str, number: int, practice: bool = False) -> tuple[list[Unit], list[Unit]]:
        label = self.labels["practice_item" if practice else "original"] + f" {number}"
        if not practice and q.get("source_label"):
            label += "  " + q["source_label"]
        units = self.heading(label, path + ".heading")
        if not practice:
            units += self.heading(self.labels["knowledge"])
            units.append(self.paragraph(self.labels["keywords"] + ": " + "; ".join(q["keywords"]), path + ".keywords"))
            for ki, text in enumerate(q["knowledge"]):
                units.append(self.paragraph(text, f"{path}.knowledge[{ki}]"))
            units.append(self.gap(3))
        units += self.blocks_from(q["question"], path + ".question")
        units += self.option_units(q.get("options", []), path + ".options")
        diagnostic = [] if practice else self.heading(self.labels["diagnostic"], path + ".diagnostic_heading") + self.blocks_from(q["diagnostic"], path + ".diagnostic")
        return units, diagnostic

    def diag_answer_units(self, pending: list[tuple[dict, str, int]]) -> list[Unit]:
        units = self.heading(self.labels["diag_answers"], large=True)
        for q, path, number in pending:
            units += self.heading(self.labels["original"] + f" {number}", path + ".diagnostic_answer_heading")
            units += self.blocks_from(q["diagnostic_answer"], path + ".diagnostic_answer")
        return units + [self.gap(5)]

    def place_long_question(self, units: list[Unit], diagnostic: list[Unit]) -> None:
        # Split at real page boundaries, not into pre-cut table fragments. That
        # keeps repeated table headers at the top of new pages only.
        while units and units[-1].kind == "spacing": units.pop()
        if not units:
            raise WorkbookError("Question has no drawable content")
        last = units.pop()
        tail_gap = self.gap(3)
        diag_height = self.plan.total([tail_gap] + diagnostic)
        available = self.height - diag_height
        if available < self.leading * 2:
            raise WorkbookError(f"{last.path}: diagnostic leaves less than two question lines on its page; use a native renderer or a shorter diagnostic")
        self.plan.add(units)
        unit = last
        while True:
            height = self.plan.size(unit)
            if height + diag_height <= self.plan.remaining + 0.02:
                self.plan.group([unit, tail_gap] + diagnostic)
                return
            if height + diag_height <= self.height + 0.02:
                self.plan.new_page()
                self.plan.group([unit, tail_gap] + diagnostic)
                return
            split_height = min(self.plan.remaining, self.height)
            # If the whole remaining block fits but the diagnostic does not,
            # split off a readable tail rather than consuming the whole block.
            if height <= split_height + 0.02:
                split_height = min(available, height - self.leading * 2)
            split = unit.flow.split(self.width, max(0, split_height))
            if len(split) < 2:
                if self.plan.page.pieces:
                    self.plan.new_page()
                    continue
                raise WorkbookError(f"{last.path}: question tail and diagnostic cannot share a page at readable size; use a native renderer")
            first = copy.copy(unit); first.flow = split[0]; first.keep_next = False
            self.plan.put(first, self.plan.size(first))
            self.plan.new_page()
            if len(split) != 2:
                raise WorkbookError(f"{last.path}: renderer returned an unsupported multi-part split; choose a native renderer")
            unit = copy.copy(unit); unit.flow = split[1]; unit.keep_next = False

    def method_units(self, cls: dict, cp: str) -> list[Unit]:
        units = self.heading(self.labels["methods"], cp + ".methods_heading", True)
        for mi, method in enumerate(cls["methods"]):
            mp = f"{cp}.methods[{mi}]"
            units += self.heading(method["title"], mp + ".title")
            units += self.blocks_from(method["content"], mp + ".content")
        if cls.get("diagnosis"):
            units += self.heading(self.labels["class_diagnosis"])
            units += self.blocks_from(cls["diagnosis"], cp + ".diagnosis")
        return units

    def solution_units(self, q: dict, qp: str, number: int, practice: bool = False) -> list[Unit]:
        label = self.labels["practice_item" if practice else "original"] + f" {number}"
        units = self.heading(label, qp + ".solution_heading")
        units.append(self.paragraph(self.labels["answer"] + ": " + q["answer"], qp + ".answer"))
        if practice:
            units += self.heading(self.labels["knowledge"])
            for ki, text in enumerate(q["knowledge"]):
                units.append(self.paragraph(text, f"{qp}.knowledge[{ki}]"))
        units += self.blocks_from(q["solution"], qp + ".solution")
        if q.get("alternatives"):
            units += self.heading(self.labels["alternative"])
            units += self.blocks_from(q["alternatives"], qp + ".alternatives")
        if q.get("diagnosis"):
            units += self.heading(self.labels["diagnosis"])
            units += self.blocks_from(q["diagnosis"], qp + ".diagnosis")
        return units

    def build_body(self) -> None:
        for ci, cls in enumerate(self.data["classes"]):
            cp = f"classes[{ci}]"
            self.plan.new_page()
            start = len(self.plan.pages)
            intro = self.heading(f"{ci + 1}. {cls['title']}", cp + ".title", True)
            if cls.get("scope"): intro += [self.paragraph(cls["scope"], cp + ".scope"), self.gap(3)]
            self.plan.add(intro)
            pending = []
            original_records = []
            methods = self.method_units(cls, cp)
            # Leave room for the methods heading and substantive opening on the final answer page.
            method_reserve = min(self.plan.total(methods), self.leading * 4 + 20)
            for qi, q in enumerate(cls["originals"]):
                self.original_index += 1
                number = self.original_index
                qp = f"{cp}.originals[{qi}]"
                record = {"id": q["id"], "class_id": cls["id"], "path": qp, "number": number,
                          "option_labels": [o["label"] for o in q.get("options", [])]}
                self.originals.append(record); original_records.append(record)
                question, diagnostic = self.question_units(q, qp, number)
                final = qi == len(cls["originals"]) - 1
                capacity = self.height - (method_reserve if final else 0)
                trial = pending + [(q, qp, number)]
                trial_answers = self.diag_answer_units(trial)
                if pending and (self.plan.total(question + diagnostic) > self.plan.remaining + 0.02 or self.plan.total(trial_answers) > capacity):
                    self.plan.new_page()
                    self.plan.add(self.diag_answer_units(pending))
                    pending = []
                    trial = [(q, qp, number)]
                    trial_answers = self.diag_answer_units(trial)
                if self.plan.total(trial_answers) > capacity + 0.02:
                    raise WorkbookError(f"{qp}.diagnostic_answer: the grouped answers cannot fit their immediate next page while preserving the methods opening; choose a native renderer or revise grouping/diagnostic length")
                intro_only = qi == 0 and len(self.plan.pages) == start
                if self.plan.total(question + diagnostic) <= self.height and not (intro_only and self.plan.total(question + diagnostic) > self.plan.remaining):
                    self.plan.group(question + diagnostic)
                else:
                    self.place_long_question(question, diagnostic)
                pending.append((q, qp, number))
            self.plan.new_page()
            self.plan.add(self.diag_answer_units(pending))
            methods_start = len(self.plan.pages)
            self.plan.add(methods)
            methods_end = len(self.plan.pages)
            solution_intro = self.heading(self.labels["solutions"], cp + ".solutions_heading", True)
            for si, (record, q) in enumerate(zip(original_records, cls["originals"])):
                units = self.solution_units(q, record["path"], record["number"])
                self.plan.add((solution_intro if si == 0 else []) + units)
            practice_intro = self.heading(self.labels["practice"], cp + ".practice_heading", True)
            practice_records = []
            for pi, q in enumerate(cls["practice"]):
                self.practice_index += 1
                pp = f"{cp}.practice[{pi}]"
                record = {"id": q["id"], "class_id": cls["id"], "path": pp, "number": self.practice_index,
                          "option_labels": [o["label"] for o in q.get("options", [])]}
                self.practice.append(record); practice_records.append(record)
                units, _ = self.question_units(q, pp, record["number"], True)
                if pi == 0:
                    units = practice_intro + units
                if self.plan.total(units) <= self.height:
                    self.plan.group(units)
                else:
                    self.plan.add(units)
            self.plan.new_page()
            answer_intro = self.heading(self.labels["practice_answers"], cp + ".practice_answers_heading", True)
            for ai, (record, q) in enumerate(zip(practice_records, cls["practice"])):
                units = self.solution_units(q, record["path"], record["number"], True)
                self.plan.add((answer_intro if ai == 0 else []) + units)
            self.classes.append({"id": cls["id"], "title": cls["title"], "path": cp,
                                 "start": start, "end": len(self.plan.pages), "methods_start": methods_start,
                                 "methods_end": methods_end})

    def make_toc(self, offset: int) -> Planner:
        toc = Planner(self.width, self.height)
        toc.add([self.paragraph(self.data["title"], "title", "title", True), self.gap(8),
                 self.paragraph(self.labels["toc"], "toc.heading", "heading", True), self.gap(5)])
        for i, cls in enumerate(self.classes):
            title = f"{i + 1}. {cls['title']}"
            text = self.paragraph(title, f"toc[{i}].title")
            page = self.paragraph(str(cls["start"] + offset), f"toc[{i}].page")
            table = Table([[text.flow, page.flow]], colWidths=[self.width - 30, 30], style=TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
            toc.add([Unit(table, f"toc[{i}]", title + " " + str(cls["start"] + offset), "toc", extra={"class_id": cls["id"]})])
        return toc

    def collect(self, pages: list[Page]) -> None:
        for pn, page in enumerate(pages, 1):
            for piece in page.pieces:
                u = piece.unit
                if not u.path: continue
                record = self.blocks.setdefault(u.path, {"path": u.path, "kind": u.kind, "text": u.text,
                                                        "pages": [], "placements": [], **u.extra})
                if pn not in record["pages"]: record["pages"].append(pn)
                record["placements"].append({"page": pn, "top_pt": round(piece.top, 3), "height_pt": round(piece.height, 3)})
                for option_path, text in zip(u.extra.get("option_paths", []), u.extra.get("option_texts", [])):
                    option = self.blocks.setdefault(option_path, {"path": option_path, "kind": "option", "text": plain_text(text), "pages": [], "placements": []})
                    if pn not in option["pages"]: option["pages"].append(pn)
                    option["placements"].append({"page": pn, "top_pt": round(piece.top, 3), "height_pt": round(piece.height, 3)})

    def range_for(self, prefix: str) -> tuple[int, int]:
        pages = [p for path, block in self.blocks.items() if path == prefix or path.startswith(prefix + "[") or path.startswith(prefix + ".") for p in block["pages"]]
        if not pages: raise WorkbookError(f"No rendered content for {prefix}")
        return min(pages), max(pages)

    def emit(self, output: Path, manifest: Path) -> dict:
        self.build_body()
        offset = 1
        for _ in range(10):
            toc = self.make_toc(offset)
            if len(toc.pages) == offset: break
            offset = len(toc.pages)
        else:
            raise WorkbookError("Contents pagination did not converge")
        pages = toc.pages + self.plan.pages
        self.collect(pages)
        for record in self.originals:
            path = record["path"]
            # Original range excludes answers and solutions.
            qpaths = [p for p in self.blocks if p.startswith(path + ".question") or p.startswith(path + ".options") or p in (path + ".heading", path + ".keywords") or p.startswith(path + ".knowledge") or p.startswith(path + ".diagnostic[") or p == path + ".diagnostic_heading"]
            rp = [p for k in qpaths for p in self.blocks[k]["pages"]]
            record["start"], record["end"] = min(rp), max(rp)
            record["question_start"], record["question_end"] = self.range_for(path + ".question")
            if record["option_labels"]:
                _, option_end = self.range_for(path + ".options")
                record["question_end"] = max(record["question_end"], option_end)
            record["diagnostic_start"], record["diagnostic_end"] = self.range_for(path + ".diagnostic")
            record["diagnostic_answer_start"], record["diagnostic_answer_end"] = self.range_for(path + ".diagnostic_answer")
            record["solution_start"], record["solution_end"] = self.range_for(path + ".solution")
            for suffix in ("answer", "alternatives", "diagnosis", "solution_heading"):
                if any(p == path + "." + suffix or p.startswith(path + "." + suffix + "[") for p in self.blocks):
                    begin, end = self.range_for(path + "." + suffix)
                    record["solution_start"] = min(record["solution_start"], begin)
                    record["solution_end"] = max(record["solution_end"], end)
        for record in self.practice:
            path = record["path"]
            record["start"], record["end"] = self.range_for(path + ".question")
            if record["option_labels"]:
                option_start, option_end = self.range_for(path + ".options")
                record["start"], record["end"] = min(record["start"], option_start), max(record["end"], option_end)
            record["answers_start"], record["answers_end"] = self.range_for(path + ".solution")
            answer_start, _ = self.range_for(path + ".answer")
            record["answers_start"] = min(answer_start, record["answers_start"])
            for suffix in ("knowledge", "alternatives", "diagnosis", "solution_heading"):
                if any(p == path + "." + suffix or p.startswith(path + "." + suffix + "[") for p in self.blocks):
                    begin, end = self.range_for(path + "." + suffix)
                    record["answers_start"] = min(record["answers_start"], begin)
                    record["answers_end"] = max(record["answers_end"], end)
        for ci, cls in enumerate(self.classes):
            for key in ("start", "end", "methods_start", "methods_end"): cls[key] += offset
            cls["toc_page"] = self.blocks[f"toc[{ci}]"]["pages"][0]
            cls["bookmark_key"] = "wb_" + hashlib.sha256(cls["id"].encode("utf-8")).hexdigest()[:20]
            originals = [r for r in self.originals if r["class_id"] == cls["id"]]
            practices = [r for r in self.practice if r["class_id"] == cls["id"]]
            cls["originals_start"] = min(r["start"] for r in originals)
            cls["originals_end"] = max(r["end"] for r in originals)
            cls["solutions_start"] = min(r["solution_start"] for r in originals)
            cls["solutions_end"] = max(r["solution_end"] for r in originals)
            cls["practice_start"] = min(r["start"] for r in practices)
            cls["practice_end"] = max(r["end"] for r in practices)
            cls["practice_answers_start"] = min(r["answers_start"] for r in practices)
            cls["practice_answers_end"] = max(r["answers_end"] for r in practices)
        output.parent.mkdir(parents=True, exist_ok=True)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_name(output.stem + ".partial.pdf")
        try:
            cv = canvas.Canvas(str(temp), pagesize=A4, pageCompression=1)
            cv.setTitle(self.data["title"]); cv.setAuthor(""); cv.setSubject(""); cv.setCreator("")
            top = A4[1] - self.margin - 16
            header = self.paragraph(self.data["title"], None)
            header_h = header.flow.wrap(self.width - 25, 15)[1]
            if header_h > 2 * self.leading:
                raise WorkbookError("Book title is too long for the running header; use a concise title")
            class_by_id = {cls["id"]: cls for cls in self.classes}
            for pn, page in enumerate(pages, 1):
                cv.setFillGray(0); cv.setStrokeGray(0)
                for cls in self.classes:
                    if cls["start"] == pn:
                        cv.bookmarkPage(cls["bookmark_key"])
                        cv.addOutlineEntry(plain_text(cls["title"]), cls["bookmark_key"], level=0, closed=False)
                if pn > 1:
                    header.flow.drawOn(cv, self.margin, A4[1] - self.margin + 1)
                footer = self.paragraph(str(pn), None)
                fw, fh = footer.flow.wrap(40, self.leading)
                footer.flow.drawOn(cv, A4[0] / 2 - 10, self.margin - self.leading)
                for piece in page.pieces:
                    piece.unit.flow.drawOn(cv, self.margin, top - piece.top - piece.height)
                    if piece.unit.kind == "toc":
                        cls = class_by_id[piece.unit.extra["class_id"]]
                        cv.linkRect("", cls["bookmark_key"],
                                    (self.margin, top - piece.top - piece.height,
                                     self.margin + self.width, top - piece.top),
                                    relative=0, thickness=0)
                cv.showPage()
            cv.save()
            os.replace(temp, output)
        finally:
            if temp.exists(): temp.unlink()
        source_bytes = json.dumps(self.data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        result = {"schema_version": 1, "renderer_version": VERSION, "data_sha256": hashlib.sha256(source_bytes).hexdigest(),
                  "pdf_sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "pdf": str(output.resolve()),
                  "page_count": len(pages), "toc_pages": list(range(1, offset + 1)), "practice_count": self.args.practice_count,
                  "layout": {"paper": "A4", "body_pt": self.body, "leading_pt": self.leading,
                             "margin_mm": self.args.margin_mm, "content_width_pt": self.width, "content_height_pt": self.height,
                             "grayscale": True}, "fonts": {"paths": self.fonts.paths, "used_names": sorted(self.fonts.used)},
                  "classes": self.classes, "originals": self.originals, "practice": self.practice,
                  "blocks": list(self.blocks.values()), "images": self.images,
                  "page_piece_counts": [len(p.pieces) for p in pages],
                  "limitations": ["Layout checks do not establish subject correctness or OCR/source authenticity.",
                                   "Visual review of final rendered pages remains required."]}
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render a reviewed, subject-neutral JSON mistake workbook to an A4 grayscale PDF. The helper checks layout, not OCR or academic truth.")
    p.add_argument("data", type=Path, help="UTF-8 JSON using references/workbook-data.md; relative image/font paths resolve beside this JSON")
    p.add_argument("--output", required=True, type=Path, help="PDF destination (existing file is replaced only after successful rendering)")
    p.add_argument("--manifest", required=True, type=Path, help="Internal JSON pagination/content evidence; keep outside the printed book")
    p.add_argument("--body-pt", type=float, help="Body font size, >=9; default data.layout.body_pt or 9.5")
    p.add_argument("--leading-pt", type=float, help="Line spacing, >=1.15*body; default data.layout.leading_pt or 12.6")
    p.add_argument("--margin-mm", type=float, default=15, help="A4 margin, default 15 mm (10-35)")
    p.add_argument("--practice-count", type=int, default=3, help="Required new questions per class, default 3; change only when user requested")
    for role in ("body", "heading", "latin", "fallback", "mono"):
        p.add_argument("--font-" + role, help=f"Override fonts.{role} with a local embeddable TrueType/TTC path")
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        data = json.loads(args.data.read_text(encoding="utf-8-sig"))
        counts = validate_data(data, args.data.resolve().parent, args.practice_count)
        result = Builder(data, args.data.resolve().parent, args).emit(args.output.resolve(), args.manifest.resolve())
        print(json.dumps({"status": "built", "pages": result["page_count"], **counts, "output": str(args.output.resolve())}, ensure_ascii=False))
        return 0
    except (WorkbookError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: renderer failed without reducing text size or omitting content: {type(exc).__name__}: {exc}. Select a suitable native renderer if this block cannot be split safely.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"): sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
