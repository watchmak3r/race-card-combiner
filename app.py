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

def extract_ragozin_numbers(rag_bytes):
    """Strict parser to pull only valid Ragozin sheet numbers from the core grid columns."""
    rag_doc = fitz.open(stream=rag_bytes, filetype="pdf")
    all_numbers = []
    
    for page in rag_doc:
        words = page.get_text("words")
        # Filter words to only look at the central body area where figures live, ignoring headers/footers
        # word format: (x0, y0, x1, y1, "word", block_no, line_no, word_no)
        body_words = [w for w in words if 50 < w[1] < 720]
        
        # Sort top-to-bottom, then left-to-right columns
        sorted_words = sorted(body_words, key=lambda w: (w[0] // 50, w[1]))
        
        for w in sorted_words:
            text = w[4].strip()
            if re.match(r'^\d{1,2}[+\-"]?$', text):
                val_num = int(re.sub(r'\D', '', text))
                if 1 <= val_num <= 40:
                    all_numbers.append(text)
                
    rag_doc.close()
    return all_numbers

def process_pdfs(drf_bytes, rag_bytes):
    try:
        drf_doc = fitz.open(stream=drf_bytes, filetype="pdf")
        ragozins = extract_ragozin_numbers(rag_bytes)
        rag_index = 0
        
        weight_pattern = re.compile(r'^L?1[1-3]\d[a-zA-Z]*$')
        
        for page in drf_doc:
            words = page.get_text("words")
            found_rows = []
            
            for w in words:
                text = w[4].strip()
                x_coord = w[0]
                
                if weight_pattern.match(text) and x_coord > 350:
                    found_rows.append({'x': x_coord, 'y': w[3]})
                    
            found_rows = sorted(found_rows, key=lambda d: d['y'])
            
            cleaned_rows = []
            for row in found_rows:
                if not cleaned_rows or (row['y'] - cleaned_rows[-1]['y'] > 8):
                    cleaned_rows.append(row)
            
            for i, row in enumerate(cleaned_rows):
                if rag_index < len(ragozins):
                    val = ragozins[rag_index]
                    
                    if i == 0:
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
                    rag_index += 1

        output_pdf = io.BytesIO()
        drf_doc.save(output_pdf)
        drf_doc.close()
        
        return output_pdf.getvalue(), None

    except Exception as e:
        error_msg = traceback.format_exc()
        return None, error_msg

if drf_file and rag_file:
    if st.button("Combine Data", type="primary"):
        with st.spinner("Extracting Ragozin sheets and aligning race card data..."):
            
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
