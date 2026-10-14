#!/usr/bin/env python3
"""Check workbook data, PDF text and recorded pagination; not academic truth/OCR.

Run visual review as well. This script cannot prove that a transcription, solution,
curriculum classification, photograph or diagnostic hypothesis is correct.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from PIL import ImageChops
from pypdf import PdfReader
from pypdf.generic import ContentStream
from reportlab.lib.pagesizes import A4

from build_workbook import AUDIT_KEYS, Fonts, WorkbookError, normalized, plain_text, validate_data


def source_hash(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def expected_texts(data: dict) -> dict[str, str]:
    """Walk the public schema independently of layout; skip all unknown audit fields."""
    texts = {"title": data["title"]}

    def blocks(values: list[dict], prefix: str) -> None:
        for i, block in enumerate(values):
            path = f"{prefix}[{i}]"
            if block["type"] in ("text", "code"):
                texts[path + ".text"] = block["text"] if block["type"] == "code" else plain_text(block["text"])
            elif block["type"] == "image" and block.get("caption"):
                texts[path + ".caption"] = plain_text(block["caption"])
            elif block["type"] == "table":
                texts[path] = "\n".join(" ".join(plain_text(c) for c in row) for row in block["rows"])

    for ci, cls in enumerate(data["classes"]):
        cp = f"classes[{ci}]"
        texts[cp + ".title"] = cls["title"]
        if cls.get("scope"): texts[cp + ".scope"] = cls["scope"]
        for mi, method in enumerate(cls["methods"]):
            mp = f"{cp}.methods[{mi}]"
            texts[mp + ".title"] = method["title"]
            blocks(method["content"], mp + ".content")
        blocks(cls.get("diagnosis", []), cp + ".diagnosis")
        for kind in ("originals", "practice"):
            for qi, q in enumerate(cls[kind]):
                qp = f"{cp}.{kind}[{qi}]"
                texts[qp + ".answer"] = q["answer"]
                for ki, text in enumerate(q["knowledge"]): texts[f"{qp}.knowledge[{ki}]"] = plain_text(text)
                for oi, opt in enumerate(q.get("options", [])):
                    texts[f"{qp}.options[{oi}]"] = plain_text(opt["label"] + ". " + opt["text"])
                for key in ("question", "solution", "alternatives", "diagnosis"):
                    blocks(q.get(key, []), qp + "." + key)
                if kind == "originals":
                    texts[qp + ".keywords"] = "; ".join(q["keywords"])
                    blocks(q["diagnostic"], qp + ".diagnostic")
                    blocks(q["diagnostic_answer"], qp + ".diagnostic_answer")
                    if q.get("source_label"):
                        texts[qp + ".heading"] = q["source_label"]
    return texts


def audit_strings(data: Any, in_audit: bool = False) -> set[str]:
    """Only metadata explicitly identified as audit data is forbidden in print.

    Do not ban words such as 'photo', 'confidence', or 'OCR' in actual questions.
    Short commonplace audit values are too ambiguous to use as string sentinels.
    """
    found = set()
    if isinstance(data, dict):
        for key, value in data.items():
            found.update(audit_strings(value, in_audit or key.lower() in AUDIT_KEYS))
    elif isinstance(data, list):
        for value in data: found.update(audit_strings(value, in_audit))
    elif in_audit and isinstance(data, str) and len(normalized(data)) >= 12:
        found.add(data)
    return found


def expected_tables(data: dict) -> dict[str, list[list[str]]]:
    tables = {}
    def walk(blocks, prefix):
        for i, block in enumerate(blocks):
            if block["type"] == "table":
                tables[f"{prefix}[{i}]"] = [[plain_text(cell) for cell in row] for row in block["rows"]]
    for ci, cls in enumerate(data["classes"]):
        cp = f"classes[{ci}]"
        for mi, method in enumerate(cls["methods"]): walk(method["content"], f"{cp}.methods[{mi}].content")
        walk(cls.get("diagnosis", []), cp + ".diagnosis")
        for kind in ("originals", "practice"):
            for qi, q in enumerate(cls[kind]):
                for key in ("question", "diagnostic", "diagnostic_answer", "solution", "alternatives", "diagnosis"):
                    walk(q.get(key, []), f"{cp}.{kind}[{qi}].{key}")
    return tables


def object_value(value):
    return value.get_object() if hasattr(value, "get_object") else value


def color_and_fonts(reader: PdfReader) -> tuple[list[str], list[str], list[dict]]:
    color_errors, font_errors, fonts = [], [], []
    visited = set()

    def inspect_stream(stream, resources, location: str) -> None:
        resources = object_value(resources) or {}
        try:
            operations = ContentStream(stream, reader).operations
        except Exception as exc:
            color_errors.append(f"{location}: cannot inspect PDF drawing operators: {exc}")
            return
        font_resources = object_value(resources.get("/Font", {}))
        used, current_font, font_stack = set(), None, []
        for operands, op in operations:
            if op in (b"rg", b"RG") and len(operands) == 3:
                values = [float(v) for v in operands]
                if max(values) - min(values) > 1e-6:
                    color_errors.append(f"{location}: non-gray RGB drawing colour {values}")
            elif op in (b"k", b"K") and len(operands) == 4:
                values = [float(v) for v in operands[:3]]
                if max(values) - min(values) > 1e-6:
                    color_errors.append(f"{location}: non-gray CMYK drawing colour")
            elif op == b"Tf":
                current_font = str(operands[0])
            elif op == b"q":
                font_stack.append(current_font)
            elif op == b"Q":
                if font_stack: current_font = font_stack.pop()
            elif op in (b"Tj", b"TJ", b"'", b'"'):
                # Canvas's initial Helvetica Tf selects a font but paints no glyphs.
                # Check fonts that actually show text, not unused default resources.
                if current_font and any(str(value) for value in operands): used.add(current_font)
            elif op in (b"cs", b"CS"):
                name = str(operands[0])
                if name not in ("/DeviceGray", "/DeviceRGB", "/DeviceCMYK"):
                    color_errors.append(f"{location}: unverified custom colour space {name}")
            elif op in (b"sc", b"SC", b"scn", b"SCN"):
                numbers = [float(v) for v in operands if isinstance(v, (int, float))]
                if len(numbers) in (3, 4) and max(numbers[:3]) - min(numbers[:3]) > 1e-6:
                    color_errors.append(f"{location}: non-gray generic drawing colour")
        for name in used:
            font = object_value(font_resources.get(name, {}))
            if not font:
                font_errors.append(f"{location}: used font {name} missing from resources"); continue
            descendants = object_value(font.get("/DescendantFonts", []))
            faces = [object_value(f) for f in descendants] if descendants else [font]
            for face in faces:
                descriptor = object_value(face.get("/FontDescriptor", {}))
                embedded = any(k in descriptor for k in ("/FontFile", "/FontFile2", "/FontFile3"))
                record = {"location": location, "resource": name, "basefont": str(face.get("/BaseFont", "")), "embedded": embedded}
                fonts.append(record)
                if not embedded: font_errors.append(f"{location}: used font {name} is not embedded")
            if not font.get("/ToUnicode"):
                font_errors.append(f"{location}: used font {name} has no Unicode text map")
        xobjects = object_value(resources.get("/XObject", {}))
        for name, obj in xobjects.items():
            obj = object_value(obj)
            if id(obj) in visited: continue
            visited.add(id(obj))
            if obj.get("/Subtype") == "/Form":
                inspect_stream(obj, obj.get("/Resources", resources), location + str(name))

    for pn, page in enumerate(reader.pages, 1):
        inspect_stream(page.get_contents(), page.get("/Resources", {}), f"page {pn}")
        try:
            for image in page.images:
                rgb = image.image.convert("RGB")
                r, g, b = rgb.split()
                if ImageChops.difference(r, g).getbbox() or ImageChops.difference(g, b).getbbox():
                    color_errors.append(f"page {pn}: image {image.name} has coloured pixels")
        except Exception as exc:
            color_errors.append(f"page {pn}: cannot inspect raster image colours: {exc}")
    return sorted(set(color_errors)), sorted(set(font_errors)), fonts


def check_navigation(reader: PdfReader, data: dict, manifest: dict, page_norm: list[str]) -> tuple[list[str], dict]:
    """Inspect actual outline destinations and link annotations, not claimed flags."""
    errors, outlines = [], []
    def flatten(items):
        for item in items:
            if isinstance(item, list):
                flatten(item)
            else:
                outlines.append(item)
    flatten(reader.outline)
    classes = data["classes"]
    if len(outlines) != len(classes):
        errors.append(f"PDF bookmark count {len(outlines)} differs from class count {len(classes)}")
    toc_pages = manifest.get("toc_pages", [])
    details = {"bookmarks": [], "toc_links": []}
    real_class_pages = []
    for ci, cls in enumerate(classes):
        heading = normalized(f"{ci + 1}. " + plain_text(cls["title"]))
        matches = [pn for pn, text in enumerate(page_norm, 1) if pn not in toc_pages and heading in text]
        real_page = matches[0] if matches else None
        real_class_pages.append(real_page)
        if real_page is None:
            errors.append(f"{cls['id']}: cannot locate the class heading in actual PDF body text")
        if ci < len(outlines):
            outline = outlines[ci]
            title = str(outline.get("/Title", ""))
            target = reader.get_destination_page_number(outline)
            target = target + 1 if target is not None and target >= 0 else None
            details["bookmarks"].append({"title": title, "page": target})
            if title != plain_text(cls["title"]):
                errors.append(f"{cls['id']}: PDF bookmark title/order differs from the class title")
            if target != real_page:
                errors.append(f"{cls['id']}: PDF bookmark target {target} differs from actual class page {real_page}")

    def destination_page(dest):
        dest = object_value(dest)
        if isinstance(dest, str):
            named = reader.named_destinations.get(dest) or reader.named_destinations.get(dest.lstrip("/"))
            if named is None: return None
            number = reader.get_destination_page_number(named)
            return number + 1 if number is not None and number >= 0 else None
        if isinstance(dest, dict):
            return destination_page(dest.get("/D"))
        if isinstance(dest, (list, tuple)) and dest:
            target = dest[0]
            for pn, page in enumerate(reader.pages, 1):
                ref = page.indirect_reference
                if hasattr(target, "idnum") and ref and target.idnum == ref.idnum and target.generation == ref.generation:
                    return pn
            target = object_value(target)
            for pn, page in enumerate(reader.pages, 1):
                if target is page: return pn
        return None

    links = []
    for pn in toc_pages:
        if not isinstance(pn, int) or not 1 <= pn <= len(reader.pages):
            errors.append("Contents navigation has an invalid PDF page"); continue
        for ref in reader.pages[pn - 1].get("/Annots", []):
            annotation = object_value(ref)
            if annotation.get("/Subtype") != "/Link": continue
            dest = annotation.get("/Dest")
            if dest is None:
                action = object_value(annotation.get("/A", {}))
                if action.get("/S") == "/GoTo": dest = action.get("/D")
            rect = [float(v) for v in annotation.get("/Rect", [])]
            target = destination_page(dest)
            links.append({"page": pn, "rect": rect, "target_page": target})
    details["toc_links"] = links
    if len(links) != len(classes):
        errors.append(f"PDF contents link count {len(links)} differs from class count {len(classes)}")
    blocks = {b["path"]: b for b in manifest.get("blocks", [])}
    margin = manifest.get("layout", {}).get("margin_mm", 15) * 72 / 25.4
    top = A4[1] - margin - 16
    for ci, cls in enumerate(classes):
        entry = blocks.get(f"toc[{ci}]", {})
        placements = entry.get("placements", [])
        if not placements:
            errors.append(f"{cls['id']}: no recorded contents row to compare with actual PDF links"); continue
        place = placements[0]
        pn = place["page"]
        if 1 <= pn <= len(page_norm) and normalized(plain_text(cls["title"])) not in page_norm[pn - 1]:
            errors.append(f"{cls['id']}: class title is missing from its actual contents page")
        x = margin + manifest["layout"]["content_width_pt"] / 2
        y = top - place["top_pt"] - place["height_pt"] / 2
        matched = [link for link in links if link["page"] == pn and len(link["rect"]) == 4 and link["rect"][0] - 0.1 <= x <= link["rect"][2] + 0.1 and link["rect"][1] - 0.1 <= y <= link["rect"][3] + 0.1]
        if len(matched) != 1:
            errors.append(f"{cls['id']}: contents row does not have exactly one actual PDF jump link")
        elif matched[0]["target_page"] != real_class_pages[ci]:
            errors.append(f"{cls['id']}: contents link target differs from actual class heading page")
    return errors, details


def check(data: dict, base: Path, pdf: Path, manifest: dict, practice_count: int) -> dict:
    errors, warnings = [], []
    counts = validate_data(data, base, practice_count)
    if manifest.get("data_sha256") != source_hash(data): errors.append("Manifest belongs to a different data revision")
    if manifest.get("pdf_sha256") != hashlib.sha256(pdf.read_bytes()).hexdigest(): errors.append("Manifest belongs to a different PDF revision")
    if manifest.get("practice_count") != practice_count: errors.append("Manifest practice count differs from the requested check")
    reader = PdfReader(str(pdf))
    if reader.is_encrypted: raise WorkbookError("Encrypted PDF cannot be checked")
    pages = [page.extract_text() or "" for page in reader.pages]
    body_text = []
    margin = manifest.get("layout", {}).get("margin_mm", 15) * 72 / 25.4
    for page in reader.pages:
        chunks = []
        def visitor(text, cm, tm, font, size):
            # Apply the text matrix to the graphics transform. Exclude running
            # headers/footers so a paragraph split over pages stays contiguous.
            # Each helper flowable is translated to its bottom-left corner.
            # pypdf's tm line advance for subset CJK fonts can be inaccurate;
            # the enclosing graphics transform remains a stable region marker.
            y = float(cm[5])
            if margin - 0.05 <= y <= A4[1] - margin - 15:
                chunks.append(text)
        page.extract_text(visitor_text=visitor)
        body_text.append("".join(chunks))
    page_norm = [normalized(t) for t in body_text]
    whole = normalized("\n".join(pages))
    total = len(pages)
    if total != manifest.get("page_count"): errors.append("PDF page count differs from manifest")
    for pn, page in enumerate(reader.pages, 1):
        box = page.mediabox
        if abs(float(box.width) - A4[0]) > 0.5 or abs(float(box.height) - A4[1]) > 0.5:
            errors.append(f"Page {pn} is not portrait A4")
        if not pages[pn - 1].strip(): errors.append(f"Page {pn} has no extractable text")
    if not manifest.get("toc_pages") or manifest["toc_pages"][0] != 1:
        errors.append("Contents must begin on PDF page 1")
    if normalized(data["title"]) not in page_norm[0]: errors.append("Book title is absent from the first contents page")
    blocks = {b["path"]: b for b in manifest.get("blocks", [])}
    expected = expected_texts(data)
    tables = expected_tables(data)
    # Source field completeness is checked independently of the renderer's claimed list.
    for path, text in expected.items():
        block = blocks.get(path)
        if block is None:
            errors.append(f"Missing rendered data field: {path}"); continue
        assigned = block.get("pages", [])
        if not assigned or any(not isinstance(p, int) or not 1 <= p <= total for p in assigned):
            errors.append(f"Invalid page range for {path}"); continue
        joined = "".join(page_norm[p - 1] for p in assigned)
        if path in tables:
            # Source rows, not manifest copies, determine required order and
            # occurrence count. Extra repeated headers at page starts are allowed.
            position = 0
            for row in tables[path]:
                row_text = normalized(" ".join(row))
                found = joined.find(row_text, position)
                if found < 0:
                    errors.append(f"PDF table is missing/reordering a source row: {path}")
                    break
                position = found + len(row_text)
            continue
        if normalized(text) not in joined:
            errors.append(f"PDF text does not contain complete field on its recorded pages: {path}")
    for path, block in blocks.items():
        for place in block.get("placements", []):
            if place["top_pt"] < -0.02 or place["height_pt"] < 0 or place["top_pt"] + place["height_pt"] > manifest["layout"]["content_height_pt"] + 0.05:
                errors.append(f"Recorded block outside page frame: {path}, page {place['page']}")
    for value in audit_strings(data):
        if normalized(value) in whole:
            errors.append("An internal source/audit metadata value appears in printed PDF text: " + value[:120])
    for cp in ("\ufffd", "\u25a1"):
        if cp in "".join(pages) and cp not in "".join(expected.values()):
            errors.append(f"Unexpected missing-glyph/replacement character U+{ord(cp):04X} in PDF")
    # Check glyph coverage against the exact font configuration saved by the builder,
    # including CLI overrides, rather than guessing from a generic font family name.
    font_config = manifest.get("fonts", {}).get("paths", {})
    try:
        fonts = Fonts(base, font_config or data.get("fonts", {}))
        for path, text in expected.items():
            for char in text: fonts.choose(char, "body", path)
    except WorkbookError as exc:
        errors.append(str(exc))
    original_map = {q["id"]: (q, f"classes[{ci}].originals[{qi}]") for ci, cls in enumerate(data["classes"]) for qi, q in enumerate(cls["originals"])}
    practice_map = {q["id"]: (q, f"classes[{ci}].practice[{qi}]") for ci, cls in enumerate(data["classes"]) for qi, q in enumerate(cls["practice"])}
    original_manifest = manifest.get("originals", [])
    practice_manifest = manifest.get("practice", [])
    if {q["id"] for q in original_manifest} != set(original_map) or len(original_manifest) != len(original_map): errors.append("Original IDs in manifest do not match the data")
    if {q["id"] for q in practice_manifest} != set(practice_map) or len(practice_manifest) != len(practice_map): errors.append("Practice IDs in manifest do not match the data")
    for record in original_manifest:
        qid = record["id"]
        if qid not in original_map: continue
        q, path = original_map[qid]
        question_places = [p for name, block in blocks.items() if name.startswith(path + ".question[") for p in block.get("placements", [])]
        knowledge_places = [p for name, block in blocks.items() if name == path + ".keywords" or name.startswith(path + ".knowledge[") for p in block.get("placements", [])]
        if question_places and knowledge_places:
            question_first = min((p["page"], p["top_pt"]) for p in question_places)
            knowledge_last = max((p["page"], p["top_pt"] + p["height_pt"]) for p in knowledge_places)
            if knowledge_last > question_first:
                errors.append(f"{qid}: knowledge/keywords do not precede the original question")
        if record["question_end"] != record["diagnostic_start"] or record["diagnostic_start"] != record["diagnostic_end"]:
            errors.append(f"{qid}: diagnostic is not entirely on the original question's final page")
        if record["diagnostic_answer_start"] != record["diagnostic_end"] + 1:
            errors.append(f"{qid}: diagnostic answer does not begin on the immediately following page")
        if record["end"] != record["diagnostic_end"]:
            errors.append(f"{qid}: original end range does not include its diagnostic")
        # All answer groups precede new originals on their shared answer page.
        answer_places = [p for name, block in blocks.items() if name.startswith(path + ".diagnostic_answer[") for p in block.get("placements", [])]
        if answer_places:
            answer_page = record["diagnostic_answer_start"]
            later_originals = [r for r in original_manifest if r["id"] != qid and r["start"] == answer_page]
            for later in later_originals:
                heading = blocks.get(later["path"] + ".heading", {})
                original_top = min((p["top_pt"] for p in heading.get("placements", []) if p["page"] == answer_page), default=1e9)
                last_answer_bottom = max((p["top_pt"] + p["height_pt"] for p in answer_places if p["page"] == answer_page), default=0)
                if original_top < last_answer_bottom - 0.05:
                    errors.append(f"{qid}: a later original precedes its diagnostic answer on page {answer_page}")
    for record in original_manifest + practice_manifest:
        mapping = original_map if record in original_manifest else practice_map
        if record["id"] not in mapping: continue
        q, path = mapping[record["id"]]
        labels = [o["label"] for o in q.get("options", [])]
        if record.get("option_labels") != labels:
            errors.append(f"{record['id']}: option labels/order differ from source data")
        previous_page, previous_pos = 0, -1
        for oi, option in enumerate(q.get("options", [])):
            block = blocks.get(f"{path}.options[{oi}]")
            if not block: continue
            pn = min(block["pages"])
            text = normalized(option["label"] + ". " + plain_text(option["text"]))
            pos = page_norm[pn - 1].find(text)
            if pos >= 0 and (pn < previous_page or (pn == previous_page and pos < previous_pos)):
                errors.append(f"{record['id']}: PDF option sequence does not preserve data order")
            if pos >= 0: previous_page, previous_pos = pn, pos
    class_ids = {c["id"] for c in data["classes"]}
    if {c["id"] for c in manifest.get("classes", [])} != class_ids:
        errors.append("Class IDs in manifest do not match data")
    for cls in manifest.get("classes", []):
        if cls["toc_page"] not in manifest["toc_pages"]: errors.append(f"{cls['id']}: contents entry outside contents pages")
        if cls["methods_start"] != cls["originals_end"] + 1: errors.append(f"{cls['id']}: methods do not start on the page immediately after all originals")
        if cls["solutions_start"] < cls["methods_start"]: errors.append(f"{cls['id']}: solutions precede methods")
        if cls["practice_start"] < cls["solutions_end"]: errors.append(f"{cls['id']}: practice precedes the final original solution")
        heading = blocks.get(cls["path"] + ".practice_heading", {})
        heading_places = heading.get("placements", [])
        original_paths = [r["path"] for r in original_manifest if r["class_id"] == cls["id"]]
        solution_places = [p for name, block in blocks.items() if any(name == prefix + ".answer" or name == prefix + ".solution_heading" or any(name.startswith(prefix + "." + key + "[") for key in ("solution", "alternatives", "diagnosis")) for prefix in original_paths) for p in block.get("placements", [])]
        if heading_places and solution_places:
            heading_first = min((p["page"], p["top_pt"]) for p in heading_places)
            solution_last = max((p["page"], p["top_pt"] + p["height_pt"]) for p in solution_places)
            if heading_first < solution_last:
                errors.append(f"{cls['id']}: practice heading occurs before the final original solution ends")
        if cls["practice_answers_start"] != cls["practice_end"] + 1: errors.append(f"{cls['id']}: practice answers do not begin immediately after all practice questions")
        for key, target in (("solutions_heading", "solutions_start"), ("practice_heading", "practice_start"), ("practice_answers_heading", "practice_answers_start")):
            section = blocks.get(cls["path"] + "." + key, {})
            if section.get("pages") and section["pages"][0] != cls[target]:
                errors.append(f"{cls['id']}: section heading {key} is separated from its first content")
        # The pre-answer practice section must not print answer knowledge or solutions.
        for record in practice_manifest:
            if record["class_id"] != cls["id"]: continue
            for name, block in blocks.items():
                if name.startswith(record["path"] + ".knowledge[") and min(block["pages"]) <= cls["practice_end"]:
                    errors.append(f"{record['id']}: practice knowledge appears before the answers section")
    if any(n <= 0 for n in manifest.get("page_piece_counts", [])): errors.append("Manifest contains an empty planned page")
    if manifest.get("layout", {}).get("body_pt", 0) < 9: errors.append("Body text is smaller than 9 pt")
    color_errors, font_errors, font_details = color_and_fonts(reader)
    errors.extend(color_errors); errors.extend(font_errors)
    navigation_errors, navigation = check_navigation(reader, data, manifest, page_norm)
    errors.extend(navigation_errors)
    for image in manifest.get("images", []):
        if image["effective_dpi"] < 120:
            warnings.append(f"{image['path']}: effective resolution {image['effective_dpi']} dpi; inspect labels and lines visually")
    return {"status": "pass" if not errors else "fail", "errors": errors, "warnings": warnings,
            "counts": {**counts, "pages": total, "checked_text_fields": len(expected)},
            "checks": {"schema_ids_options": True, "text_completeness": not any("field" in e for e in errors),
                       "pagination": not any("page" in e or "precede" in e for e in errors),
                       "grayscale": not color_errors, "used_fonts_embedded_unicode": not font_errors,
                       "actual_pdf_navigation": not navigation_errors},
            "fonts": font_details, "navigation": navigation,
            "scope": "Automated data, text, font, grayscale, PDF navigation and recorded-pagination checks only.",
            "limitations": ["Does not establish academic correctness, OCR/source authenticity, or the truth of diagnostic hypotheses.",
                             "Page text and manifest cannot prove visual absence of overlap; inspect final rendered pages."]}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Check a workbook JSON, its PDF and matching layout manifest. This is not an OCR, subject-answer or visual-layout certification.")
    p.add_argument("data", type=Path, help="Reviewed workbook JSON")
    p.add_argument("--pdf", required=True, type=Path, help="PDF to check")
    p.add_argument("--manifest", required=True, type=Path, help="Matching internal build manifest")
    p.add_argument("--report", required=True, type=Path, help="Write the check report outside the printed book")
    p.add_argument("--practice-count", type=int, default=3, help="Expected new questions per class, default 3; match an explicitly requested override")
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        data = json.loads(args.data.read_text(encoding="utf-8-sig"))
        manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
        report = check(data, args.data.resolve().parent, args.pdf, manifest, args.practice_count)
    except Exception as exc:
        report = {"status": "fail", "errors": [f"{type(exc).__name__}: {exc}"], "warnings": [],
                  "limitations": ["This checker does not establish academic correctness or OCR/source authenticity."]}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "errors": len(report["errors"]), "warnings": len(report["warnings"]),
                      "report": str(args.report.resolve())}, ensure_ascii=False))
    if report["errors"]:
        for error in report["errors"][:20]: print("ERROR: " + error, file=sys.stderr)
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"): sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
