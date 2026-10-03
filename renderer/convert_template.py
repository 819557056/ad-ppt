"""Renderer-container entry point: static PPTX -> per-page PNG.

Run only inside a restricted, no-network renderer container or an equivalent
per-job sandbox. The API/worker refuses PPTX import without an explicit wrapper.
"""
import subprocess
import sys
import tempfile
import re
import unicodedata
import zipfile
from pathlib import Path

import fitz
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException


_SLIDE_XML = re.compile(r'^ppt/slides/slide[0-9]+\.xml$')
_DRAWING = '{http://schemas.openxmlformats.org/drawingml/2006/main}'


def _font_key(name):
    return ' '.join(unicodedata.normalize('NFKC', name).casefold().split())


def _explicit_slide_fonts(source):
    """Read only literal font declarations, not theme aliases or example text."""
    names = set()
    scanned_bytes = 0
    try:
        with zipfile.ZipFile(source) as archive:
            for entry in archive.infolist():
                if not _SLIDE_XML.fullmatch(entry.filename):
                    continue
                scanned_bytes += entry.file_size
                if entry.file_size > 8 * 1024 * 1024 or scanned_bytes > 32 * 1024 * 1024:
                    return None
                root = ElementTree.fromstring(archive.read(entry))
                for element in root.iter():
                    if element.tag in (_DRAWING + 'latin', _DRAWING + 'ea', _DRAWING + 'cs'):
                        family = element.attrib.get('typeface', '').strip()
                        if family and not family.startswith('+'):
                            names.add(_font_key(family))
    except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError,
            DefusedXmlException):
        return None
    return names


def _installed_font_families():
    try:
        result = subprocess.run(['fc-list', '-f', '%{family}\n'], capture_output=True,
                                text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return {_font_key(family) for line in result.stdout.splitlines()
            for family in line.split(',') if family.strip()}


def font_warnings(source):
    declared = _explicit_slide_fonts(source)
    installed = _installed_font_families()
    if declared is None or installed is None:
        return ['FONT_AVAILABILITY_UNVERIFIED']
    if declared - installed:
        return ['FONT_FAMILY_UNAVAILABLE']
    return []


def main(source, output, *, pdf_only=False):
    source = Path(source).resolve()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    version = subprocess.run(['soffice', '--version'], capture_output=True,
                             text=True, timeout=10)
    if version.returncode == 0:
        print(version.stdout.strip())
    warnings = font_warnings(source)
    with tempfile.TemporaryDirectory(prefix='scene-office-') as temp:
        profile = Path(temp) / 'profile'
        result = subprocess.run(['soffice', f'-env:UserInstallation={profile.as_uri()}',
            '--headless', '--convert-to', 'pdf', '--outdir', str(temp), str(source)],
            capture_output=True, timeout=120)
        pdf = Path(temp) / (source.stem + '.pdf')
        if result.returncode != 0 or not pdf.is_file():
            raise RuntimeError('LibreOffice conversion failed')
        if pdf_only:
            if pdf.stat().st_size > 100 * 1024 * 1024:
                raise RuntimeError('LibreOffice verification PDF exceeds limit')
            (output / 'output.pdf').write_bytes(pdf.read_bytes())
        else:
            with fitz.open(pdf) as doc:
                for index, page in enumerate(doc, 1):
                    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                    (output / f'page-{index:03d}.png').write_bytes(pix.tobytes('png'))
    print('SCENE_RENDER_WARNINGS=' + ','.join(warnings))
    return 0


if __name__ == '__main__':
    if len(sys.argv) not in (3, 4) or (len(sys.argv) == 4 and sys.argv[3] != '--pdf-only'):
        raise SystemExit('Usage: convert_template.py source.pptx output_dir [--pdf-only]')
    raise SystemExit(main(sys.argv[1], sys.argv[2], pdf_only=len(sys.argv) == 4))
