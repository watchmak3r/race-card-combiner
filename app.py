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
    """Extracts valid Ragozin figures from a single horse's Ragozin page in proper layout order."""
    words = rag_page.get_text("words")
    # Filter out page headers/footers
    body_words = [w for w in words if 40 < w[1] < 730]
    # Sort top-to-bottom, then left-to-right
    sorted_words = sorted(body_words, key=lambda w: (w[1] // 15, w[0]))
    
    page_numbers = []
    for w in sorted_words:
        text = w[4].strip()
        if re.match(r'^\d{1,2}[+\-"]?$', text):
            val_num = int(re.sub(r'\D', '', text))
            if 1 <= val_num <= 45:
                page_numbers.append(text)
    return page_numbers

def process_pdfs(drf_bytes, rag_bytes):
    try:
        drf_doc = fitz.open(stream=drf_bytes, filetype="pdf")
        rag_doc = fitz.open(stream=rag_bytes, filetype="pdf")
        
        rag_page_idx = 0
        date_pattern = re.compile(r'^\d{1,2}[A-Za-z]{3}\d{2}')
        horse_header_pattern = re.compile(r'^\d+\s+[A-Z]')

        for page in drf_doc:
            words = page.get_text("words")
            
            # Find all horse header positions and past performance date rows on this page
            elements = []
            for w in words:
                text = w[4].strip()
                x_coord = w[0]
                
                if x_coord < 70:
                    if date_pattern.match(text):
                        elements.append({'type': 'pp_row', 'y': w[3]})
                    elif horse_header_pattern.match(text):
                        elements.append({'type': 'horse_header', 'y': w[3]})
            
            # Group rows by horse block or process sequentially
            # Since each horse block has past performance rows, we map the next Ragozin page per horse header encountered
            current_horse_rows = []
            horses_data = []
            
            for el in sorted(elements, key=lambda e: e['y']):
                if el['type'] == 'horse_header':
                    if current_horse_rows:
                        horses_data.append(current_horse_rows)
                        current_horse_rows = []
                elif el['type'] == 'pp_row':
                    current_horse_rows.append(el['y'])
            if current_horse_rows:
                horses_data.append(current_horse_rows)

            # For each horse found on the DRF page, pull the corresponding Ragozin sheet page
            for pp_y_coords in horses_data:
                horse_rag_numbers = []
                if rag_page_idx < len(rag_doc):
                    horse_rag_numbers = extract_ragozin_page_numbers(rag_doc[rag_page_idx])
                    rag_page_idx += 1
                
                # Clean up duplicate/overlapping Y coordinates for safety
                cleaned_y = []
                for y in pp_y_coords:
                    if not cleaned_y or (y - cleaned_y[-1] > 8):
                        cleaned_y.append(y)

                # Inject the horse's specific Ragozin figures next to its past performance rows
                for idx, y_val in enumerate(cleaned_y):
                    if idx < len(horse_rag_numbers):
                        val = horse_rag_numbers[idx]
                        page.insert_text(
                            fitz.Point(18, y_val - 2),
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
