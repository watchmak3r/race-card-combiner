import streamlit as st
import fitz
import re
import io
import traceback
from datetime import date

# Configure iPad-Friendly UI Layout
st.set_page_config(page_title="Race Card Combiner", layout="wide")
st.markdown("""
    <style>
    section[data-testid="stFileUploader"] { padding: 1.5rem 0; }
    section[data-testid="stFileUploader"] > div > div {
        min-height: 250px; display: flex; flex-direction: column;
        justify-content: center; align-items: center; border-width: 3px; border-radius: 15px;
    }
    div[data-testid="stButton"] > button, div[data-testid="stDownloadButton"] > button {
        min-height: 80px; width: 100%; font-size: 24px !important;
        font-weight: bold; border-radius: 15px;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🏇 Race Card Combiner")
st.markdown("Drag and drop your DRF Past Performances and Ragozin Sheets below.")

col1, col2 = st.columns(2)
with col1:
    drf_file = st.file_uploader("1. DRF PPs (PDF)", type="pdf", key="drf")
with col2:
    rag_file = st.file_uploader("2. Ragozin Sheets (PDF)", type="pdf", key="rag")


# =============================================================================
# RAGOZIN PARSER
# On a Ragozin sheet the month of each race is not printed. It is encoded by the
# line's vertical position (Dec at top, Jan at bottom), and the year by which
# column the line is in. Each part of a race line also uses its own font, which
# is how we tell the figure apart from the day of month, flags, etc.
# =============================================================================

MONTH_BAND_HEIGHT = 43.125        # 12 month bands, Dec (top) to Jan (bottom)
BAND_TOP_FROM_DEC_LABEL = 17      # band top sits 17pt above the "D" label baseline
LINE_TOP_FROM_BASELINE = 7
FIGURE_FONTS = {"W1HotDog", "R2Sq1", "Rs30Bold", "NewCenturySchlbk-Roman", "CaxExBold1", "HV3-Normal"}
FOREIGN_SUFFIXES = "IRE|IR|GB|FR|GER|SAF|SA|BRZ|ARG|CHI|JPN|AUS|NZ|CAN|ITY|PER|URU"

# Ragozin 2-letter track code -> DRF track abbreviation (uppercase)
RAG_TO_DRF_TRACK = {
    "AQ": "AQU", "Sr": "SAR", "BE": "BEL", "CD": "CD", "GP": "GP", "KE": "KEE", "LR": "LRL",
    "FG": "FG", "Pd": "PID", "CN": "CNL", "MT": "MTH", "DE": "DEL", "TA": "TAM", "OP": "OP",
    "EL": "ELP", "TP": "TP", "SA": "SA", "KD": "KD", "DM": "DMR", "FL": "FL", "IN": "IND",
    "PX": "PRX", "PE": "PEN", "WO": "WO", "PI": "PIM", "GG": "GG", "CT": "CT",
    "BT": "BTP", "PR": "PRM", "ME": "MEY",   # best guesses
}


def span_role(font, size, text):
    if font == "Courier-Bold":
        return "monthLabel"
    if font == "Helvetica" and size < 4:
        return "noise"
    if font == "H7":
        return "prefix"
    if font == "ZapfDingbats":
        return "synthetic"
    if font == "AftSym-Bold":
        return "flags"
    if font == "A2Gross":
        return "classTrack"
    if font == "Helvetica-Narrow":
        if size < 6.5:
            return "pedigree"
        if size < 7.5:
            return "day"
        return "colHeaderOrNote"
    if font == "Helvetica-Bold":
        if 7.5 < size < 8.5:
            return "header"
        if 6.5 < size < 6.95 and re.fullmatch(r"[A-Za-z0-9]{3}", text):
            return "trainerCode"
        return "figure"
    if font == "NewCenturySchlbk-Italic":
        return "currentTrainer"
    if font == "HV3-Normal" and size < 7:
        return "note"
    if font in FIGURE_FONTS:
        return "figure"
    return "other"


def figure_to_number(base, mod):
    """Ragozin: '-' quarter point better, '+' quarter worse, '"' half worse."""
    if not base.isdigit():
        return None
    n = float(base)
    return {"-": n - 0.25, "+": n + 0.25, '"': n + 0.5}.get(mod, n)


def normalize_name(name):
    n = name.upper().replace("’", "").replace("'", "").replace("`", "")
    n = re.sub(rf"\s*\(({FOREIGN_SUFFIXES})\)", "", n)
    n = re.sub(rf"-({FOREIGN_SUFFIXES})\b", "", n)
    n = re.sub(r"[^A-Z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def parse_ragozin_page(page):
    spans = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for s in line["spans"]:
                text = s["text"].strip()
                if not text:
                    continue
                font = s["font"].split("+")[-1]
                spans.append({
                    "s": text, "x": s["bbox"][0], "y": s["origin"][1],
                    "font": font, "size": s["size"],
                    "role": span_role(font, s["size"], text),
                })

    horse = {"name": None, "lines": [], "notes": [], "warnings": []}

    # ---- Header ----
    top = [i for i in spans if i["y"] < 80]
    title = " ".join(i["s"] for i in sorted(top, key=lambda i: i["x"])
                     if i["role"] == "header" and i["x"] > 200)
    m = re.match(r"^(.*?)\s+([FMCGHR])\s+(\d{2})\s+(\S+)\s+Race\s+(\d+)", re.sub(r"\s+", " ", title))
    if m:
        horse.update(name=m.group(1), sex=m.group(2), foal_year=2000 + int(m.group(3)),
                     today_distance=m.group(4), race_number=int(m.group(5)))
    else:
        horse["warnings"].append(f"Could not read title: {title!r}")

    # ---- Grid geometry ----
    d_label = min((i for i in spans if i["role"] == "monthLabel" and i["s"].startswith("D")),
                  key=lambda i: i["y"], default=None)
    grid_top = (d_label["y"] if d_label else 107) - BAND_TOP_FROM_DEC_LABEL

    year_cols = []
    for i in spans:
        if i["role"] == "colHeaderOrNote":
            cm = re.fullmatch(r"(\d+)\s+RACES?\s+(\d{2})", i["s"])
            if cm:
                year_cols.append({"x": i["x"], "year": 2000 + int(cm.group(2)), "count": int(cm.group(1))})
    if not year_cols:
        return horse

    def nearest_col(x):
        return min(year_cols, key=lambda c: abs(c["x"] - x))

    # ---- Rows: group spans by baseline, split each row into race lines ----
    grid = [i for i in spans if i["y"] > 88 and i["role"] not in
            ("noise", "monthLabel", "header", "currentTrainer", "pedigree")]
    y_rows = []
    for it in sorted(grid, key=lambda i: i["y"]):
        row = next((r for r in y_rows if abs(r["y"] - it["y"]) <= 3.5), None)
        if row is None:
            row = {"y": it["y"], "items": []}
            y_rows.append(row)
        row["items"].append(it)

    segments = []
    for r in y_rows:
        cur = []
        for it in sorted(r["items"], key=lambda i: i["x"]):
            cur.append(it)
            if it["role"] in ("day", "trainerCode") and any(c["role"] == "classTrack" for c in cur):
                segments.append({"y": r["y"], "items": cur, "col": None})
                cur = []
        for it in cur:   # leftovers: notes, or a race line with no class token
            col = nearest_col(it["x"] - 20)
            seg = next((s for s in segments if s["y"] == r["y"] and s["col"] is col), None)
            if seg is None:
                seg = {"y": r["y"], "items": [], "col": col}
                segments.append(seg)
            seg["items"].append(it)

    raw_lines = []
    for seg in segments:
        its = sorted(seg["items"], key=lambda i: i["x"])
        ct = next((i for i in its if i["role"] == "classTrack"), None)
        text = " ".join(i["s"] for i in its)
        foreign = None
        if not ct and any(i["role"] in ("figure", "prefix") for i in its):
            foreign = re.match(r'^(.*?)(\d{1,2})\s*([+\-"]?)\s+([A-Z]{2,3})$', text)
        col = nearest_col(ct["x"] - 30) if ct else (seg["col"] or nearest_col(its[0]["x"] - 20))
        if not ct and not foreign:
            horse["notes"].append({"year": col["year"], "text": text})
            continue

        if foreign:
            left_str = foreign.group(1) + foreign.group(2) + foreign.group(3)
        else:
            left_str = "".join(i["s"] for i in its if i["x"] < ct["x"] and i["role"] != "flags")
        fm = re.match(r'^(.*?)(\d{1,2}|XX)\s*([+\-"]?)$', left_str)
        ctm = re.match(r"^(.*?)([A-Za-z]{2})$", ct["s"]) if ct else None
        right = [i for i in its if ct and i["x"] > ct["x"]]
        day_item = next((i for i in right if i["role"] == "day"), None)
        trainer_item = next((i for i in right if i["role"] == "trainerCode"), None)

        # Month from the vertical band; day from position inside the band (day 1 at bottom)
        anchor = day_item or trainer_item or ct or its[0]
        band_pos = (anchor["y"] - LINE_TOP_FROM_BASELINE - grid_top) / MONTH_BAND_HEIGHT
        band_idx = min(11, max(0, int(band_pos)))
        month = 12 - band_idx
        year = col["year"]
        dim = days_in_month(year, month)
        est_day = min(dim, max(1, round((1 - (band_pos - band_idx)) * dim) + 1))
        day = int(day_item["s"]) if day_item else None
        if day is not None and day - est_day > 15 and month > 1:
            month -= 1   # date right on a month edge, printed day settles it
        if day is not None and est_day - day > 15 and month < 12:
            month += 1
        final_day = min(day if day is not None else est_day, days_in_month(year, month))

        raw_lines.append({
            "_y": seg["y"],
            "date": date(year, month, final_day),
            "day_estimated": day is None,
            "figure_raw": (fm.group(2) + fm.group(3)) if fm else None,
            "figure": figure_to_number(fm.group(2), fm.group(3)) if fm else None,
            "prefix": fm.group(1) if fm else left_str,
            "turf": bool(fm and "=" in fm.group(1)),
            "synthetic": any(i["role"] == "synthetic" for i in its),
            "flags": "".join(i["s"] for i in its if i["role"] == "flags"),
            "race_class": ctm.group(1) if ctm else (ct["s"] if ct else None),
            "rag_track": ctm.group(2) if ctm else (foreign.group(4) if foreign else None),
            "trainer_change": trainer_item["s"] if trainer_item else None,
        })

    # Newest first, same order as DRF past performances
    raw_lines.sort(key=lambda l: (-l["date"].year, l["_y"]))
    for l in raw_lines:
        del l["_y"]
    horse["lines"] = raw_lines
    return horse


def days_in_month(year, month):
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def parse_ragozin_pdf(rag_doc):
    return [parse_ragozin_page(p) for p in rag_doc]


# =============================================================================
# DRF side helpers
# =============================================================================

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
DRF_DATE_RE = re.compile(r"(\d{1,2})(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(\d{2})\s*(?:\d{1,2}\s*)?([A-Z][A-Za-z]{1,3})?")


def words_on_line(words, y_bottom, max_x=None, tol=3):
    ws = [w for w in words if abs(w[3] - y_bottom) <= tol and (max_x is None or w[0] < max_x)]
    return " ".join(w[4] for w in sorted(ws, key=lambda w: w[0]))


def parse_drf_row_date(text):
    m = DRF_DATE_RE.search(text)
    if not m:
        return None, None
    try:
        d = date(2000 + int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
    except ValueError:
        return None, None
    return d, (m.group(4) or "").upper()


def match_line_for_row(rag_lines, used, row_date, row_track):
    """Exact date wins (any track). Estimated-day lines may be off by up to 3 days at the same track."""
    best, best_score = None, None
    for idx, l in enumerate(rag_lines):
        if idx in used:
            continue
        diff = abs((l["date"] - row_date).days)
        drf_track = RAG_TO_DRF_TRACK.get(l["rag_track"])
        track_ok = (drf_track is None or not row_track or drf_track == row_track)
        if l["day_estimated"]:
            if diff > 3 or not track_ok:
                continue
        elif diff != 0:
            continue
        score = diff + (0 if track_ok else 0.5)
        if best_score is None or score < best_score:
            best, best_score = idx, score
    return best


def process_pdfs(drf_bytes, rag_bytes):
    report = []
    try:
        drf_doc = fitz.open(stream=drf_bytes, filetype="pdf")
        rag_doc = fitz.open(stream=rag_bytes, filetype="pdf")

        rag_horses = parse_ragozin_pdf(rag_doc)
        rag_by_name = {}
        for i, h in enumerate(rag_horses):
            if h["name"]:
                rag_by_name.setdefault(normalize_name(h["name"]), i)
        used_horses = set()
        next_rag_idx = 0          # fallback: next Ragozin page in order

        weight_pattern = re.compile(r'^L?1[1-3]\d[a-zA-Z]*$')
        horse_header_pattern = re.compile(r'^\d+\s+[A-Z]')

        current = None            # state for the horse currently being filled (can span pages)

        for page_num, page in enumerate(drf_doc, 1):
            words = page.get_text("words")

            elements = []
            for w in words:
                text = w[4].strip()
                x_coord = w[0]
                if weight_pattern.match(text) and x_coord > 350:
                    elements.append({'type': 'pp_row', 'x': x_coord, 'y': w[3]})
                elif horse_header_pattern.match(text) and x_coord < 70:
                    elements.append({'type': 'horse_header', 'x': x_coord, 'y': w[3]})

            # Group rows by horse; rows before the first header continue the previous page's horse
            groups = []
            group = {'header_y': None, 'rows': []}
            for el in sorted(elements, key=lambda e: e['y']):
                if el['type'] == 'horse_header':
                    if group['rows'] or group['header_y'] is not None:
                        groups.append(group)
                    group = {'header_y': el['y'], 'rows': []}
                else:
                    group['rows'].append({'x': el['x'], 'y': el['y']})
            if group['rows'] or group['header_y'] is not None:
                groups.append(group)

            for g in groups:
                if g['header_y'] is not None:
                    # New horse: find its Ragozin sheet by name, else take the next one in order
                    header_text = normalize_name(words_on_line(words, g['header_y'], tol=4))
                    rag_idx, method = None, None
                    for name, idx in rag_by_name.items():
                        if idx not in used_horses and re.search(rf"\b{re.escape(name)}\b", header_text):
                            rag_idx, method = idx, "name"
                            break
                    if rag_idx is None:
                        while next_rag_idx < len(rag_horses) and next_rag_idx in used_horses:
                            next_rag_idx += 1
                        if next_rag_idx < len(rag_horses):
                            rag_idx, method = next_rag_idx, "page order"
                    if rag_idx is not None:
                        used_horses.add(rag_idx)
                        next_rag_idx = max(next_rag_idx, rag_idx + 1)
                    current = {
                        'horse': rag_horses[rag_idx] if rag_idx is not None else None,
                        'used_lines': set(), 'order_pos': 0,
                        'report': {'DRF page': page_num, 'DRF header': header_text[:40],
                                   'Ragozin horse': rag_horses[rag_idx]["name"] if rag_idx is not None else "NONE",
                                   'matched by': method or "-", 'rows': 0, 'figures placed': 0,
                                   'row matching': ""},
                    }
                    report.append(current['report'])
                if current is None or current['horse'] is None:
                    continue

                rag_lines = current['horse']['lines']

                cleaned_rows = []
                for row in g['rows']:
                    if not cleaned_rows or (row['y'] - cleaned_rows[-1]['y'] > 8):
                        cleaned_rows.append(row)

                for row in cleaned_rows:
                    current['report']['rows'] += 1
                    first_row_of_horse = current['report']['rows'] == 1
                    row_date, row_track = parse_drf_row_date(words_on_line(words, row['y'], max_x=row['x']))

                    if row_date is not None:
                        current['report']['row matching'] = "by date"
                        idx = match_line_for_row(rag_lines, current['used_lines'], row_date, row_track)
                    else:
                        # No readable date: fall back to newest-first order
                        current['report']['row matching'] = "by order (no DRF date found)"
                        idx = current['order_pos'] if current['order_pos'] < len(rag_lines) else None
                        current['order_pos'] += 1
                    if idx is None:
                        continue
                    current['used_lines'].add(idx)
                    val = rag_lines[idx]['figure_raw']
                    if not val:
                        continue

                    # Right-align the figure where the old 1-2 digit numbers ended
                    right_edge = row['x'] - (14 if first_row_of_horse else 10)
                    inject_x = right_edge - fitz.get_text_length(val, fontname="hebo", fontsize=9)
                    page.insert_text(
                        fitz.Point(inject_x, row['y'] - 2),
                        val,
                        fontsize=9,
                        fontname="hebo",
                        color=(1, 0, 0)
                    )
                    current['report']['figures placed'] += 1

        output_pdf = io.BytesIO()
        drf_doc.save(output_pdf)
        drf_doc.close()
        rag_doc.close()

        return output_pdf.getvalue(), None, report, rag_horses

    except Exception:
        return None, traceback.format_exc(), report, None


if drf_file and rag_file:
    if st.button("Combine Data", type="primary"):
        with st.spinner("Extracting Ragozin sheets per horse and aligning race card data..."):

            combined_pdf_bytes, error_message, report, rag_horses = process_pdfs(
                drf_file.getvalue(), rag_file.getvalue())

            if error_message:
                st.error("🚨 Encountered a processing error. Please share this output:")
                st.code(error_message)
            else:
                original_name = drf_file.name if drf_file else "RaceCard.pdf"
                output_filename = f"Combo_{original_name}"

                st.success("Successfully mapped, parsed, and injected!")
                st.download_button(
                    label="📥 Download Combined PDF",
                    data=combined_pdf_bytes,
                    file_name=output_filename,
                    mime="application/pdf"
                )

                with st.expander("Matching details (for troubleshooting)"):
                    st.dataframe(report, use_container_width=True)
                with st.expander("Parsed Ragozin sheets"):
                    for h in rag_horses:
                        st.markdown(f"**{h['name']}** (Race {h.get('race_number', '?')})")
                        st.dataframe([
                            {"date": l["date"].isoformat(), "fig": l["figure_raw"],
                             "track": l["rag_track"], "class": l["race_class"],
                             "day est.": l["day_estimated"]}
                            for l in h["lines"]
                        ], use_container_width=True)
