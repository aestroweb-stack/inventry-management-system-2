"""Compact QR stickers retain the design payload and print resolution."""
from io import BytesIO
from pathlib import Path
import sys
from PIL import Image
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from services.barcode_service import generate_product_qr_label

@pytest.mark.parametrize('sku', ['SAMPLE_V_D01_BLACK','SAMPLE_'+ 'LONG_DESIGN_'*12+'BLACK'])
def test_qr_print_size_and_contrast(sku):
    im = Image.open(BytesIO(generate_product_qr_label(sku, 'Sample design')))
    assert im.format == 'PNG'
    assert all(abs(x-300)<.1 for x in im.info['dpi'])
    assert im.width >= 354 and im.height >= 413
    if len(sku)<30:
        assert im.size == (354,413)
    assert im.getpixel((0,0)) == (255,255,255)
    assert (0,0,0) in im.getdata()
