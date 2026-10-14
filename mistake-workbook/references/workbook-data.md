# Workbook data and renderer

Use this schema when the bundled PDF helper fits the content. The JSON is an internal work product, not a printed appendix. All paths resolve relative to the JSON file. There are no subject-specific years, question counts, answer letters, or source repositories built into the format.

```json
{
  "title": "错题集",
  "language": "zh-CN",
  "layout": {"body_pt": 9.5, "leading_pt": 12.6},
  "fonts": {"body": "optional/path/to/font.ttf", "heading": "optional/path/to/bold.ttf", "latin": "optional/path/to/latin.ttf", "fallback": "optional/path/to/unicode.ttf"},
  "classes": [
    {
      "id": "unique-concept-id",
      "title": "具体知识与思维操作",
      "scope": "optional verified curriculum scope",
      "originals": [
        {
          "id": "unique-question-id",
          "source_label": "optional year or exam name; never photo coordinates",
          "keywords": ["概念", "操作", "边界条件"],
          "knowledge": ["解释考查的知识、成立条件以及本题要求完成的思维操作。"],
          "question": [{"type": "text", "text": "完整原题。"}],
          "options": [{"label": "A", "text": "选项内容"}],
          "diagnostic": [{"type": "text", "text": "区分具体卡点的小问。"}],
          "diagnostic_answer": [{"type": "text", "text": "答案、理由与判断标准。"}],
          "answer": "答案或参考结论，不要求为选择题字母",
          "solution": [{"type": "text", "text": "完整推导/证据/评分要点。"}],
          "alternatives": [{"type": "text", "text": "适用条件不同的有效解题角度。"}],
          "diagnosis": [{"type": "text", "text": "具体易错思维及如何通过小问确认。"}]
        }
      ],
      "methods": [
        {"title": "解题角度名称", "content": [{"type": "text", "text": "为何有效、适用条件、操作步骤、局限。"}]}
      ],
      "diagnosis": [{"type": "text", "text": "本类如何区分概念、表征、策略、推理与执行卡点。"}],
      "practice": [
        {
          "id": "unique-practice-id",
          "question": [{"type": "text", "text": "没有知识提示或答案线索的新题。"}],
          "options": [],
          "answer": "明确答案或参考答案/评价量规",
          "knowledge": ["新题考查的知识及适用条件，仅出现在答案页。"],
          "solution": [{"type": "text", "text": "解析或完整评分量规。"}],
          "alternatives": [],
          "diagnosis": []
        }
      ]
    }
  ]
}
```

`practice` defaults to exactly three questions per class. Change the number only when the user requests it; communicate the choice in chat. Options may have any labels/count or be absent. A source label is optional; do not invent one when only a mock exercise is known.

If the user explicitly changes the practice count, pass the same `--practice-count N` to both helpers. They validate the requested count rather than silently dropping or inventing questions.

Content blocks:

- `text`: plain UTF-8 `text`. Newlines are preserved. Limited explicit formatting may use `<b>`, `<i>`, `<super>`, `<sub>`, `<br/>`; comparison signs such as `x<0` remain literal text.
- `image`: `path`, optional `width_pt`, `height_pt`, optional meaningful `caption`. Specified dimensions form a bounding box; the image keeps its aspect ratio. Use sharp verified diagrams/equations, not screenshots of whole questions with grading marks. Prefer vector figures where the chosen renderer supports them.
- `table`: rectangular `rows` (strings), optional `header_rows` (0 by default), optional `widths` as relative positive column weights.
- `code`: literal `text`; preserve spacing and line breaks for programming or technical questions.

For complex mathematical typography, non-Latin complex scripts, large tables or specialist diagrams, use a suitable native renderer rather than flattening meaning to fit this helper. The skill's content and pagination requirements still apply. Images of equations require a readable text equivalent in the internal audit ledger, even when the printed representation is visual.

The helper has Chinese and English section labels. Use a localized native layout when another book language is required; `language` must not silently replace the user's language with English labels.

## Internal audit ledger

Keep source details separately in `work/`: input image path and crop/page region, transcription confidence and unresolved tokens, duplicate relationships, source matches and option mappings, independent answer verification, mistake evidence, and hypothesis confidence. Never include this ledger, OCR list, confidence labels, source-coordinate tags or production notes in the final book.

## Execution

Use a Python interpreter with ReportLab, Pillow and pypdf. Prefer the environment's bundled runtime when available. `fonts` are optional on Windows with ordinary system fonts; on other systems provide available embeddable font paths if automatic discovery fails. Do not distribute proprietary fonts with the skill.

Resolve `scripts/` against this skill's installation directory and invoke those absolute script paths. Resolve the JSON, output and manifest against the current task directory; keep task data and deliverables outside the installed skill.

```text
python scripts/build_workbook.py work/workbook.json --output outputs/错题集.pdf --manifest work/layout.json
python scripts/check_workbook.py work/workbook.json --pdf outputs/错题集.pdf --manifest work/layout.json --report work/checks.json
```

The helper is responsible for layout, not OCR, academic correctness, curriculum discovery, or generating teaching content. Populate and independently review the full data before rendering. Verify the produced PDF visually, including all changed pages after a revision.
