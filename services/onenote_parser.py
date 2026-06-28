import os
import tempfile
import subprocess
from typing import List

def extract_onenote_pages_as_pdfs(file_path: str) -> List[str]:
    """
    Reads a .one or .onepkg file, uses PowerShell to drive OneNote COM,
    exports each page as a PDF, and returns a list of paths.
    Using PowerShell bypasses pywin32 'AttributeError' and 'Library not registered' bugs.
    """
    if not file_path.lower().endswith((".one", ".onepkg")):
        raise ValueError("Unsupported file format. Only .one and .onepkg are supported.")

    output_dir = tempfile.mkdtemp(prefix="onenote_out_")
    
    ps_script = f"""
$ErrorActionPreference = "Stop"

try {{
    $onenote = New-Object -ComObject OneNote.Application.15
}} catch {{
    try {{
        $onenote = New-Object -ComObject OneNote.Application
    }} catch {{
        Write-Error "Failed to start OneNote COM application."
        exit 1
    }}
}}

$filePath = "{file_path}"
$outputDir = "{output_dir}"

[ref]$objectId = ""

try {{
    if ($filePath.ToLower().EndsWith(".onepkg")) {{
        $destPath = Join-Path $outputDir "Package"
        New-Item -ItemType Directory -Force -Path $destPath | Out-Null
        $onenote.OpenPackage($filePath, $destPath, [ref]$objectId)
    }} else {{
        $onenote.OpenHierarchy($filePath, "", [ref]$objectId, 0)
    }}
}} catch {{
    Write-Error "Failed to open OneNote file: $($_.Exception.Message)"
    exit 1
}}

[ref]$xmlOut = ""
$onenote.GetHierarchy($objectId.Value, 2, $xmlOut)

[xml]$doc = $xmlOut.Value
$ns = New-Object System.Xml.XmlNamespaceManager($doc.NameTable)
$ns.AddNamespace("one", "http://schemas.microsoft.com/office/onenote/2013/onenote")

$pages = $doc.SelectNodes("//one:Page", $ns)
if ($pages.Count -eq 0) {{
    $ns.AddNamespace("one", "http://schemas.microsoft.com/office/onenote/2010/onenote")
    $pages = $doc.SelectNodes("//one:Page", $ns)
}}

foreach ($page in $pages) {{
    $pageId = $page.ID
    $guid = [guid]::NewGuid().ToString()
    $pdfPath = Join-Path $outputDir "$guid.pdf"
    
    try {{
        # 2 = pfPDF
        $onenote.Publish($pageId, $pdfPath, 2, "")
        Write-Output $pdfPath
    }} catch {{
        Write-Error "Failed to publish page: $($_.Exception.Message)"
    }}
}}

try {{
    $onenote.CloseNotebook($objectId.Value)
}} catch {{}}
"""

    ps1_path = os.path.join(output_dir, "extract.ps1")
    with open(ps1_path, "w", encoding="utf-8") as f:
        f.write(ps_script)

    result = subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", ps1_path],
        capture_output=True
    )

    # デコード処理 (Shift-JIS/cp932環境でもエラーにならないように)
    try:
        stdout_str = result.stdout.decode("utf-8")
        stderr_str = result.stderr.decode("utf-8")
    except UnicodeDecodeError:
        stdout_str = result.stdout.decode("cp932", errors="replace")
        stderr_str = result.stderr.decode("cp932", errors="replace")

    if result.returncode != 0:
        raise RuntimeError(f"PowerShell extraction failed: {stderr_str}\nOutput: {stdout_str}")

    pdf_paths = []
    for line in stdout_str.strip().splitlines():
        line = line.strip()
        if line.lower().endswith(".pdf") and os.path.exists(line):
            pdf_paths.append(line)

    return pdf_paths

