import streamlit as st
import fitz
import re
import io
import traceback
import unicodedata
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


def _clean(n):
    # Strip accents (Señor -> SENOR), apostrophes, and punctuation
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode()
    n = n.upper().replace("'", "").replace("`", "")
    return n


def name_keys(name):
    """All reasonable keys for a name. Covers hyphenated names that also carry a
    country tag (e.g. TWENTY-ONE-IR), where stripping once vs twice differs."""
    n = _clean(name.replace("’", "'"))
    had_drf_tag = bool(re.search(r"\([A-Z]{2,4}\)\s*$", n))
    n = re.sub(r"\s*\([A-Z]{2,4}\)\s*$", "", n)
    keys = {re.sub(r"[^A-Z0-9]", "", n)}
    if not had_drf_tag:                 # only Ragozin-style names carry -IR / -BR tags
        keys.add(re.sub(r"[^A-Z0-9]", "", re.sub(r"-[A-Z]{2,3}$", "", n)))
    return keys


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
# DRF side: find each horse and its PP rows, paste Ragozin figures in order
# =============================================================================

WEIGHT_PATTERN = re.compile(r'^L?1[1-3]\d[a-zA-Z]*$')


DATE_AT_LEFT = re.compile(r'^(\d{1,2})\D(\d{2})')     # DRF row date, e.g. 22Aug26 (month is a symbol in the PDF)
WEIGHT_AT_END = re.compile(r'L?1[1-3]\d[a-z]{0,3}$')


def find_drf_horses_and_rows(page):
    """Horse names are the big 13.8pt bold text at the left margin.
    A PP row is any line that starts with a race date at the left edge, which
    covers US and foreign races and ignores comment lines like '110yds'.
    The figure is placed just left of that row's weight (L126 / 126)."""
    names = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for s in line["spans"]:
                t = s["text"].strip()
                if t and 13 <= s["size"] <= 15 and s["bbox"][0] < 50:
                    names.append({"name": t, "y": s["bbox"][3]})

    words = page.get_text("words")
    rows = []
    for d in words:
        if d[0] >= 40 or not DATE_AT_LEFT.match(d[4]):
            continue
        same_line = [v for v in words if abs(v[3] - d[3]) < 2]
        weight_x = None
        for v in sorted(same_line, key=lambda v: v[0]):
            if 350 < v[0] < 395 and WEIGHT_PATTERN.match(v[4]):
                weight_x = v[0]
                break
        glued = False
        if weight_x is None:
            # Weight glued onto a long name, e.g. "Soumillon126"
            for v in same_line:
                m = WEIGHT_AT_END.search(v[4])
                if m and 365 < v[2] < 395 and len(v[4]) > len(m.group()):
                    weight_x = v[2] - fitz.get_text_length(m.group(), fontname="helv", fontsize=8.5) * 0.55
                    glued = True
                    break
        if weight_x is None:
            weight_x = 369
        # Foreign rows: a trainer name can run underneath the weight. Stop before it.
        crossing = [v[0] for v in same_line if v[0] < weight_x - 1 and v[2] > weight_x + 12]
        x = min(crossing + [weight_x])
        left = [v[2] for v in same_line if 250 < v[2] <= x + 0.5]
        if glued:
            left.append(x)                    # the name runs right up to the weight
        dm = DATE_AT_LEFT.match(d[4])
        rows.append({"x": x, "y": d[3], "text_end": max(left) if left else x - 30,
                     "day": int(dm.group(1)), "year": 2000 + int(dm.group(2))})

    rows.sort(key=lambda r: r["y"])
    cleaned = []
    for r in rows:
        if not cleaned or r["y"] - cleaned[-1]["y"] > 4:
            cleaned.append(r)
    return sorted(names, key=lambda n: n["y"]), cleaned


def next_figure(current, row):
    """Figures go down the PP rows in order. One safety check: the row's year and
    day of month (readable even though DRF hides the month) must agree with the
    Ragozin line. That keeps European horses lined up, since Ragozin leaves some
    overseas races off the sheet entirely."""
    lines = current["lines"]
    pos = current["pos"]
    # Skip Ragozin lines newer than this row (races DRF doesn't show)
    while pos < len(lines) and lines[pos]["date"].year > row["year"]:
        pos += 1
    # Within the same year, find the line for this day (estimated days can be off a bit)
    for j in range(pos, len(lines)):
        l = lines[j]
        if l["date"].year != row["year"]:
            break
        gap = abs(l["date"].day - row["day"])
        if l["day_estimated"]:
            gap = min(gap, 31 - gap)          # an estimate near a month edge can wrap (31st vs 1st)
        if gap <= (3 if l["day_estimated"] else 0):
            current["pos"] = j + 1
            return l["figure_raw"]
    current["pos"] = pos
    return None          # race not on the Ragozin sheet: leave the row blank


def process_pdfs(drf_bytes, rag_bytes):
    report = []
    try:
        drf_doc = fitz.open(stream=drf_bytes, filetype="pdf")
        rag_doc = fitz.open(stream=rag_bytes, filetype="pdf")

        rag_horses = parse_ragozin_pdf(rag_doc)
        rag_by_name = {}          # key -> list of sheet indexes (same name can appear twice)
        for i, h in enumerate(rag_horses):
            if h["name"]:
                for k in name_keys(h["name"]):
                    rag_by_name.setdefault(k, []).append(i)

        def find_sheet(drf_name, race_no):
            cands = []
            for k in name_keys(drf_name):
                cands += [i for i in rag_by_name.get(k, []) if i not in cands]
            if not cands:
                return None, "no sheet"
            # Prefer the sheet for this race (handles two horses with the same name,
            # e.g. one US-bred and one Irish-bred), then any sheet not already used
            same_race = [i for i in cands if rag_horses[i].get("race_number") == race_no]
            unused = [i for i in (same_race or cands) if i not in used]
            pick = (unused or same_race or cands)[0]
            return pick, "name"
        used = set()
        current = None              # horse being filled; can continue onto the next page

        for page_num, page in enumerate(drf_doc, 1):
            names, rows = find_drf_horses_and_rows(page)
            rm = re.search(r"race\s+(\d+),\s*page", page.get_text())
            race_no = int(rm.group(1)) if rm else None
            name_iter = iter(names)
            next_name = next(name_iter, None)

            for row in rows:
                # Start a new horse whenever we pass a horse name
                while next_name is not None and next_name["y"] < row["y"]:
                    idx, method = find_sheet(next_name["name"], race_no)
                    if idx is not None:
                        used.add(idx)
                    current = {
                        "lines": rag_horses[idx]["lines"] if idx is not None else [],
                        "pos": 0,
                        "report": {"DRF page": page_num, "DRF horse": next_name["name"],
                                   "Ragozin sheet": rag_horses[idx]["name"] if idx is not None else "NONE",
                                   "race": race_no,
                                   "matched by": method, "PP rows": 0, "figures placed": 0},
                    }
                    report.append(current["report"])
                    next_name = next(name_iter, None)

                if current is None:
                    continue
                current["report"]["PP rows"] += 1
                val = next_figure(current, row)
                if not val:
                    continue

                # Figure sits right up against the weight column (L126). On the few
                # rows where a long jockey name runs that far, a white backing keeps
                # the number readable.
                m = re.match(r'^(\d+|XX)(.*)$', val)
                digits, modifier = (m.group(1), m.group(2)) if m else (val, "")
                digits_w = fitz.get_text_length(digits, fontname="hebo", fontsize=9)
                mod_w = fitz.get_text_length(modifier, fontname="hebo", fontsize=7) if modifier else 0
                right_edge = row["x"] - 1.5
                inject_x = right_edge - digits_w - (mod_w + 0.2 if modifier else 0)
                if inject_x < row["text_end"] + 1:
                    page.draw_rect(
                        fitz.Rect(inject_x - 0.8, row["y"] - 8.5, right_edge + 0.3, row["y"] - 0.5),
                        color=None, fill=(1, 1, 1), overlay=True)
                page.insert_text(
                    fitz.Point(inject_x, row["y"] - 2),
                    digits,
                    fontsize=9,
                    fontname="hebo",
                    color=(1, 0, 0)
                )
                if modifier:
                    page.insert_text(
                        fitz.Point(inject_x + digits_w + 0.2, row["y"] - 2),
                        modifier,
                        fontsize=7,
                        fontname="hebo",
                        color=(1, 0, 0)
                    )
                current["report"]["figures placed"] += 1

        output_pdf = io.BytesIO()
        drf_doc.save(output_pdf)
        drf_doc.close()
        rag_doc.close()

        # Things worth a human look
        unmatched_sheets = [h["name"] for i, h in enumerate(rag_horses) if i not in used]
        no_sheet_with_rows = [r["DRF horse"] for r in report
                              if r["matched by"] == "no sheet" and r["PP rows"] > 0]
        stats = {"unmatched_sheets": unmatched_sheets,
                 "no_sheet_with_rows": no_sheet_with_rows,
                 "rag_horses": len(rag_horses),
                 "rag_lines": sum(len(h["lines"]) for h in rag_horses),
                 "drf_horses": len(report),
                 "placed": sum(r["figures placed"] for r in report)}
        return output_pdf.getvalue(), None, report, rag_horses, stats

    except Exception:
        return None, traceback.format_exc(), report, None, None


if drf_file and rag_file:
    if st.button("Combine Data", type="primary"):
        with st.spinner("Extracting Ragozin sheets per horse and aligning race card data..."):

            combined_pdf_bytes, error_message, report, rag_horses, stats = process_pdfs(
                drf_file.getvalue(), rag_file.getvalue())

            if error_message:
                st.error("🚨 Encountered a processing error. Please share this output:")
                st.code(error_message)
            else:
                original_name = drf_file.name if drf_file else "RaceCard.pdf"
                output_filename = f"Combo_{original_name}"

                st.success(
                    f"Read {stats['rag_lines']} race lines from {stats['rag_horses']} Ragozin sheets. "
                    f"Found {stats['drf_horses']} horses in the DRF. Placed {stats['placed']} figures.")
                if stats['no_sheet_with_rows']:
                    st.warning("These horses have past races but no Ragozin sheet was found for them "
                               "(check the spelling on both sheets): " + ", ".join(stats['no_sheet_with_rows']))
                if stats['unmatched_sheets']:
                    st.warning("These Ragozin sheets didn't match any horse in the DRF: "
                               + ", ".join(stats['unmatched_sheets']))
                if stats['placed'] == 0:
                    st.warning("No figures were placed. Open the troubleshooting sections below "
                               "and send a screenshot so the cause can be pinned down.")
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
