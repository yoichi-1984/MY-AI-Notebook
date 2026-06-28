import os
import zipfile
import tempfile
import uuid
import xml.etree.ElementTree as ET
from typing import List

def extract_onenote_pages_as_pdfs(file_path: str) -> List[str]:
    """
    Reads a .one or .onepkg file, uses OneNote COM to export each page as a PDF,
    and returns a list of paths to the generated PDF files.
    """
    try:
        import win32com.client
    except ImportError:
        raise ImportError("pywin32 is not installed. Please install it to use OneNote features.")

    output_dir = tempfile.mkdtemp(prefix="onenote_out_")
    
    extracted_one_files = []
    zip_temp_dir = None
    
    if file_path.lower().endswith(".onepkg"):
        zip_temp_dir = tempfile.mkdtemp(prefix="onenote_zip_")
        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            zip_ref.extractall(zip_temp_dir)
        for root, _, files in os.walk(zip_temp_dir):
            for file in files:
                if file.lower().endswith(".one"):
                    extracted_one_files.append(os.path.join(root, file))
    elif file_path.lower().endswith(".one"):
        extracted_one_files.append(file_path)
    else:
        raise ValueError("Unsupported file format. Only .one and .onepkg are supported.")

    pdf_paths = []
    
    if not extracted_one_files:
        return pdf_paths

    try:
        # OneNote Application object
        onenote = win32com.client.Dispatch("OneNote.Application")
    except Exception as e:
        raise RuntimeError(f"Failed to start OneNote application via COM: {e}")

    for one_file in extracted_one_files:
        try:
            # 1. Open the .one file (Section)
            section_id = onenote.OpenHierarchy(os.path.abspath(one_file), "", "", 2)
            
            # 2. Get Hierarchy for pages
            # HierarchyScope 2 = hsPages
            xml_out = onenote.GetHierarchy(section_id, 2, "")
            
            # 3. Parse XML to find pages
            root = ET.fromstring(xml_out)
            # Namespace handling for OneNote XML (e.g. {http://schemas.microsoft.com/office/onenote/2013/onenote}Page)
            ns_match = root.tag.split('}')
            ns = {'one': ns_match[0].strip('{')} if len(ns_match) > 1 else {}
            
            pages = root.findall('.//one:Page', ns) if ns else root.findall('.//Page')
            for page in pages:
                page_id = page.get('ID')
                if not page_id:
                    continue
                
                # Create a unique filename for the PDF
                pdf_path = os.path.join(output_dir, f"{uuid.uuid4().hex}.pdf")
                
                # 4. Publish as PDF
                # PublishFormat 2 = pfPDF
                onenote.Publish(page_id, pdf_path, 2, "")
                
                if os.path.exists(pdf_path):
                    pdf_paths.append(pdf_path)
                    
        except Exception as e:
            print(f"Error processing {one_file}: {e}")
            continue

    return pdf_paths
