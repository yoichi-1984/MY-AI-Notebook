import os
import tempfile
import subprocess
import json
from typing import List, Dict, Any

def extract_onenote_pages_native(file_path: str) -> List[Dict[str, Any]]:
    """
    Reads a .one or .onepkg file, uses PowerShell to drive OneNote COM,
    extracts native text, images, and attachments directly from the OneNote XML,
    and returns a list of dictionaries per page.
    """
    if not file_path.lower().endswith((".one", ".onepkg")):
        raise ValueError("Unsupported file format. Only .one and .onepkg are supported.")

    output_dir = tempfile.mkdtemp(prefix="onenote_out_")
    
    ps_script = f"""
$ErrorActionPreference = 'Stop'
$onenote = $null
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

try {{
    $filePath = "{file_path}"
    $outputDir = "{output_dir}"

    $rawPath = Join-Path $outputDir "Package"
    New-Item -ItemType Directory -Force -Path $rawPath | Out-Null

    if ($filePath.ToLower().EndsWith(".onepkg")) {{
        $output = & expand.exe -D "$filePath"
        $cabFiles = @()
        foreach ($line in $output) {{
            if ($line -match ':\s*([^:]+\.one\w*)$') {{
                $cabFiles += $matches[1].Trim()
            }}
        }}
        
        if ($cabFiles.Count -eq 1) {{
            $destFile = Join-Path $rawPath $cabFiles[0]
            & expand.exe "$filePath" "$destFile" | Out-Null
        }} else {{
            & expand.exe -F:* "$filePath" "$rawPath" | Out-Null
        }}

        if ($LASTEXITCODE -ne 0) {{
            Write-Error "expand.exe failed with exit code $LASTEXITCODE"
            exit 1
        }}
    }} else {{
        $fileName = [System.IO.Path]::GetFileName($filePath)
        Copy-Item "$filePath" (Join-Path $rawPath $fileName)
    }}

    $notebookId = ""
    try {{
        $onenote.OpenHierarchy($rawPath, "", [ref]$notebookId, 0)
    }} catch {{
        Write-Error "Failed to OpenHierarchy on directory: $($_.Exception.Message)"
        exit 1
    }}

    $hierarchyDoc = $null
    $pages = $null

    # Wait for OneNote to asynchronously index the pages
    for ($attempt = 0; $attempt -lt 20; $attempt++) {{
        $hierarchyXml = ""
        try {{
            $onenote.GetHierarchy($notebookId, 4, [ref]$hierarchyXml)
            [xml]$hierarchyDoc = $hierarchyXml
            $ns = New-Object System.Xml.XmlNamespaceManager($hierarchyDoc.NameTable)
            $ns.AddNamespace("one", "http://schemas.microsoft.com/office/onenote/2013/onenote")
            
            $pages = $hierarchyDoc.SelectNodes(".//*[local-name()='Page']", $ns)
            if ($pages -and $pages.Count -gt 0) {{
                break
            }}
        }} catch {{}}
        Start-Sleep -Milliseconds 500
    }}

    if (-not $pages -or $pages.Count -eq 0) {{
        Write-Error "Found 0 pages in $filePath."
        try {{ $onenote.CloseNotebook($notebookId) }} catch {{}}
        exit 0
    }}

    foreach ($page in $pages) {{
        $pageId = $page.GetAttribute("ID")
        if (-not $pageId) {{ $pageId = $page.ID }}
        $pageName = $page.GetAttribute("name")
        if (-not $pageName) {{ $pageName = $page.name }}

        $sectionName = "Unknown Section"
        if ($page.ParentNode -and $page.ParentNode.LocalName -eq "Section") {{
            $sectionName = $page.ParentNode.GetAttribute("name")
        }}

        $pageXml = ""
        try {{
            # piBinaryData = 1, xs2013 = 2
            $onenote.GetPageContent($pageId, [ref]$pageXml, 1, 2)
            [xml]$pDoc = $pageXml
            
            # Extract Text
            $outLines = @()
            $oes = $pDoc.SelectNodes('.//one:OE', $ns)
            foreach ($oe in $oes) {{
                $oeTexts = @()
                foreach ($t in $oe.SelectNodes('.//one:T', $ns)) {{
                    $rawText = $t.InnerText
                    $rawText = $rawText -replace '(?i)<br\s*/?>', [Environment]::NewLine
                    $rawText = $rawText -replace '<[^>]+>', ''
                    $rawText = [System.Net.WebUtility]::HtmlDecode($rawText)
                    $oeTexts += $rawText
                }}
                if ($oeTexts.Count -gt 0) {{
                    $outLines += ($oeTexts -join '')
                }}
            }}
            $pageText = ($outLines -join [Environment]::NewLine)

            # Extract Images
            $images = $pDoc.SelectNodes('.//one:Image', $ns)
            $extractedImages = @()
            foreach ($img in $images) {{
                $data = $img.SelectSingleNode('one:Data', $ns)
                if ($data) {{
                    $ext = $img.extension
                    if (-not $ext) {{ $ext = "png" }}
                    $imgPath = Join-Path $outputDir ("img_" + [guid]::NewGuid().ToString() + "." + $ext)
                    [System.IO.File]::WriteAllBytes($imgPath, [System.Convert]::FromBase64String($data.InnerText))
                    $extractedImages += $imgPath
                }}
            }}

            # Extract Attachments
            $files = $pDoc.SelectNodes('.//one:InsertedFile', $ns)
            $extractedFiles = @()
            foreach ($file in $files) {{
                $cachePath = $file.GetAttribute('pathCache')
                if ($cachePath -and (Test-Path $cachePath)) {{
                    $name = $file.GetAttribute('preferredName')
                    if (-not $name) {{ $name = "attached_" + [guid]::NewGuid().ToString() + ".bin" }}
                    $filePathAttachment = Join-Path $outputDir $name
                    Copy-Item -Path $cachePath -Destination $filePathAttachment -Force
                    $extractedFiles += $filePathAttachment
                }}
            }}

            $resultObj = @{{
                pageId = $pageId
                pageName = $pageName
                sectionName = $sectionName
                text = $pageText
                images = $extractedImages
                attachments = $extractedFiles
            }}
            Write-Output "JSON_OUTPUT_START"
            $resultObj | ConvertTo-Json -Depth 5 -Compress
            Write-Output "JSON_OUTPUT_END"
            
        }} catch {{
            Write-Error "Failed to process page $pageId : $($_.Exception.Message)"
        }}
    }}

    try {{
        $onenote.CloseNotebook($notebookId)
    }} catch {{}}

}} finally {{
    if ($onenote -ne $null) {{
        try {{
            [Runtime.InteropServices.Marshal]::ReleaseComObject($onenote) | Out-Null
        }} catch {{}}
    }}
}}
"""

    ps1_path = os.path.join(output_dir, "extract.ps1")
    with open(ps1_path, "w", encoding="utf-8") as f:
        f.write(ps_script)

    result = subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", ps1_path],
        capture_output=True
    )

    try:
        stdout_str = result.stdout.decode("utf-8")
        stderr_str = result.stderr.decode("utf-8")
    except UnicodeDecodeError:
        stdout_str = result.stdout.decode("cp932", errors="replace")
        stderr_str = result.stderr.decode("cp932", errors="replace")

    if result.returncode != 0:
        raise RuntimeError(f"PowerShell extraction failed: {stderr_str}\nOutput: {stdout_str}")

    pages_data = []
    lines = stdout_str.strip().splitlines()
    in_json = False
    json_lines = []

    for line in lines:
        line = line.strip()
        if line == "JSON_OUTPUT_START":
            in_json = True
            json_lines = []
            continue
        if line == "JSON_OUTPUT_END":
            in_json = False
            try:
                pages_data.append(json.loads("".join(json_lines)))
            except json.JSONDecodeError as e:
                print(f"[Warning] Failed to parse JSON output: {e}")
            continue
            
        if in_json:
            json_lines.append(line)

    if not pages_data and "Found 0 pages" not in stdout_str:
        raise RuntimeError(f"No pages extracted from OneNote.\nOutput: {stdout_str}\nStderr: {stderr_str}")

    # Attach the temp directory to the first page so workflow can clean it up later
    if pages_data:
        pages_data[0]["_temp_dir"] = output_dir

    return pages_data
