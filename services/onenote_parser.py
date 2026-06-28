import os
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

    if not file_path.lower().endswith((".one", ".onepkg")):
        raise ValueError("Unsupported file format. Only .one and .onepkg are supported.")

    output_dir = tempfile.mkdtemp(prefix="onenote_out_")
    pdf_paths = []

    try:
        # OneNote Application object
        onenote = win32com.client.Dispatch("OneNote.Application")
    except Exception as e:
        raise RuntimeError(f"Failed to start OneNote application via COM: {e}")

    try:
        # 1. Open the file directly using COM. 
        # CreateFileType 0 = cftNone (Auto-detect). OneNote will unpack .onepkg automatically or open .one
        hierarchy_id = onenote.OpenHierarchy(os.path.abspath(file_path), "", "", 0)
        
        # 2. Get Hierarchy for pages. HierarchyScope 2 = hsPages
        xml_out = onenote.GetHierarchy(hierarchy_id, 2, "")
        
        # 3. Parse XML to find pages
        root = ET.fromstring(xml_out)
        # Namespace handling for OneNote XML
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
        print(f"Error processing {file_path}: {e}")
    finally:
        # OneNoteアプリ内に追加された一時的なノートブックやセクションを閉じてユーザー環境を綺麗に保つ
        try:
            if 'hierarchy_id' in locals():
                onenote.CloseNotebook(hierarchy_id)
        except:
            pass

    return pdf_paths
