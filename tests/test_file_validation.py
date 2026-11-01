import io
import uuid

import pytest
from PIL import Image

from app.core.exceptions import FileTooLargeError, InvalidFileError, UnsupportedFileTypeError
from app.services.file_service import FileStore, sanitize_filename, validate_upload
from app.utils.sample_invoice import pdf_to_png, render_pdf, valid_intra_state

MB = 1024 * 1024


@pytest.fixture(scope="module")
def pdf_bytes():
    return render_pdf(valid_intra_state())


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (50, 50), "white").save(buf, format="JPEG")
    return buf.getvalue()


def test_valid_pdf_png_jpg_accepted(pdf_bytes):
    assert validate_upload("inv.pdf", pdf_bytes, 10 * MB).kind == "pdf"
    assert validate_upload("inv.png", pdf_to_png(pdf_bytes, 72), 10 * MB).kind == "png"
    assert validate_upload("inv.JPG", _jpeg(), 10 * MB).kind == "jpeg"


@pytest.mark.parametrize("name", ["invoice.exe", "invoice.txt", "invoice", "invoice.pdf.js"])
def test_unsupported_extensions_rejected(name, pdf_bytes):
    with pytest.raises(UnsupportedFileTypeError):
        validate_upload(name, pdf_bytes, 10 * MB)


def test_empty_file_rejected():
    with pytest.raises(InvalidFileError):
        validate_upload("a.pdf", b"", 10 * MB)


def test_too_large_rejected(pdf_bytes):
    with pytest.raises(FileTooLargeError):
        validate_upload("a.pdf", pdf_bytes, max_bytes=100)


def test_extension_content_mismatch_rejected(pdf_bytes):
    with pytest.raises(InvalidFileError, match="does not match"):
        validate_upload("invoice.png", pdf_bytes, 10 * MB)


def test_non_invoice_content_rejected():
    with pytest.raises(InvalidFileError, match="not a valid"):
        validate_upload("invoice.pdf", b"MZ\x90\x00 executable bytes", 10 * MB)


def test_corrupted_pdf_rejected():
    with pytest.raises(InvalidFileError, match="corrupted"):
        validate_upload("invoice.pdf", b"%PDF-1.7\n garbage garbage", 10 * MB)


def test_corrupted_png_rejected():
    with pytest.raises(InvalidFileError, match="corrupted"):
        validate_upload("invoice.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, 10 * MB)


def test_truncated_jpeg_rejected():
    data = _jpeg()
    with pytest.raises(InvalidFileError):
        validate_upload("invoice.jpg", data[: len(data) // 3], 10 * MB)


@pytest.mark.parametrize("raw,expected", [
    ("../../etc/passwd.pdf", "passwd.pdf"),
    ("..\\..\\windows\\system32\\a.pdf", "a.pdf"),
    ("my invoice (1).pdf", "my invoice _1_.pdf"),
    (None, "invoice"),
])
def test_filename_sanitised(raw, expected):
    assert sanitize_filename(raw) == expected


def test_file_store_only_accepts_server_ids(tmp_path):
    store = FileStore(tmp_path)
    good = str(uuid.uuid4())
    path = store.save(good, "pdf", b"%PDF-")
    assert path.parent == tmp_path.resolve()
    with pytest.raises(ValueError):
        store.path_for("../../evil", ".pdf")
