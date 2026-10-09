import streamlit as st
import fitz
import re
import io
import traceback

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

def extract_ragozin_page_numbers(rag_page):
    """Extracts Ragozin figures, ignoring the upcoming race reference line at the top."""
    words = rag_page.get_text("words")
    # Filter out page headers, footers, and the top header line representing the upcoming race
    body_words = [w for w in words if 70 < w[1] < 730]
    
    # Sort right-to-left by year columns (-round(w[0] / 80)), then top-to-bottom vertically (w[1])
    sorted_words = sorted(body_words, key=lambda w: (-round(w[0] / 80), w[1]))
    
    page_numbers = []
    seen_pos = set()
    for w in sorted_words:
        raw_text = w[4].strip()
        digits_match = re.search(r'\d{1,2}', raw_text)
        if digits_match:
            val_num = int(digits_match.group())
            if 1 <= val_num <= 45:
                pos_key = (round(w[0] / 10), round(w[1] / 10))
                if pos_key not in seen_pos:
                    page_numbers.append(str(val_num))
                    seen_pos.add(pos_key)
                    
    return page_numbers

def process_pdfs(drf_bytes, rag_bytes):
    try:
        drf_doc = fitz.open(stream=drf_bytes, filetype="pdf")
        rag_doc = fitz.open(stream=rag_bytes, filetype="pdf")
        
        rag_page_idx = 0
        weight_pattern = re.compile(r'^L?1[1-3]\d[a-zA-Z]*$')
        horse_header_pattern = re.compile(r'^\d+\s+[A-Z]')

        for page in drf_doc:
            words = page.get_text("words")
            
            elements = []
            for w in words:
                text = w[4].strip()
                x_coord = w[0]
                
                if weight_pattern.match(text) and x_coord > 350:
                    elements.append({'type': 'pp_row', 'x': x_coord, 'y': w[3]})
                elif horse_header_pattern.match(text) and x_coord < 70:
                    elements.append({'type': 'horse_header', 'x': x_coord, 'y': w[3]})
            
            current_horse_rows = []
            horses_data = []
            
            for el in sorted(elements, key=lambda e: e['y']):
                if el['type'] == 'horse_header':
                    if current_horse_rows:
                        horses_data.append(current_horse_rows)
                        current_horse_rows = []
                elif el['type'] == 'pp_row':
                    current_horse_rows.append({'x': el['x'], 'y': el['y']})
            if current_horse_rows:
                horses_data.append(current_horse_rows)

            for pp_rows in horses_data:
                horse_rag_numbers = []
                if rag_page_idx < len(rag_doc):
                    # Skip the first extracted number if it represents the upcoming race reference line
                    all_extracted = extract_ragozin_page_numbers(rag_doc[rag_page_idx])
                    horse_rag_numbers = all_extracted[1:] if len(all_extracted) > 0 else []
                    rag_page_idx += 1
                
                cleaned_rows = []
                for row in pp_rows:
                    if not cleaned_rows or (row['y'] - cleaned_rows[-1]['y'] > 8):
                        cleaned_rows.append(row)

                for idx, row in enumerate(cleaned_rows):
                    if idx < len(horse_rag_numbers):
                        val = horse_rag_numbers[idx]
                        
                        if idx == 0:
                            inject_x = row['x'] - 24
                        else:
                            if len(val) > 1:
                                inject_x = row['x'] - 20
                            else:
                                inject_x = row['x'] - 16
                                
                        inject_y = row['y'] - 2 
                        
                        page.insert_text(
                            fitz.Point(inject_x, inject_y),
                            val,
                            fontsize=9,
                            fontname="hebo",
                            color=(1, 0, 0)
                        )

        output_pdf = io.BytesIO()
        drf_doc.save(output_pdf)
        drf_doc.close()
        rag_doc.close()
        
        return output_pdf.getvalue(), None

    except Exception as e:
        error_msg = traceback.format_exc()
        return None, error_msg

if drf_file and rag_file:
    if st.button("Combine Data", type="primary"):
        with st.spinner("Extracting Ragozin sheets per horse and aligning race card data..."):
            
            combined_pdf_bytes, error_message = process_pdfs(drf_file.getvalue(), rag_file.getvalue())
            
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
