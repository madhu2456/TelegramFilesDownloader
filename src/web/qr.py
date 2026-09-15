"""Pure-Python SVG QR code generator for TeleVault."""
from __future__ import annotations

import io

import qrcode
import qrcode.image.svg


def generate_qr_svg(url: str) -> str:
    """Generate SVG representation of a QR code using pure-Python qrcode.

    Uses SvgPathImage to avoid requiring PIL/Pillow or C-extension image libraries.
    """
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2,
            image_factory=qrcode.image.svg.SvgPathImage,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image()
        stream = io.BytesIO()
        img.save(stream)
        return stream.getvalue().decode("utf-8")
    except Exception:
        return ""
