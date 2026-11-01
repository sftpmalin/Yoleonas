"""QR code utility: generate previews and downloads in memory."""
from __future__ import annotations

import base64
from io import BytesIO
import os
import stat
from xml.etree import ElementTree

from flask import Blueprint, jsonify, render_template, request
from PIL import Image
import qrcode
from qrcode.exceptions import DataOverflowError
from qrcode.image.svg import SvgPathFillImage
from werkzeug.exceptions import RequestEntityTooLarge


qr_code_bp = Blueprint("qr_code_bp", __name__, url_prefix="/qr-code")
EXPORT_SIZES = (256, 512, 1024)
CORRECTIONS = {
    "L": qrcode.constants.ERROR_CORRECT_L,
    "M": qrcode.constants.ERROR_CORRECT_M,
    "Q": qrcode.constants.ERROR_CORRECT_Q,
    "H": qrcode.constants.ERROR_CORRECT_H,
}
MAX_CONTENT_BYTES = 7089
# Allow a BOM and up to four bytes per character for Unicode text files.
MAX_FILE_BYTES = MAX_CONTENT_BYTES * 4 + 4
ElementTree.register_namespace("", "http://www.w3.org/2000/svg")


def build_qr(text: str, correction: str = "M") -> qrcode.QRCode:
    if not isinstance(text, str) or not text:
        raise ValueError("Saisis un texte, un lien ou une suite de numéros.")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Le texte contient un caractère Unicode invalide.") from None
    if len(encoded) > MAX_CONTENT_BYTES:
        raise ValueError("Ce texte est trop long pour un seul QR code.")
    if not isinstance(correction, str) or correction not in CORRECTIONS:
        raise ValueError("Choisis un niveau de correction L, M, Q ou H.")

    code = qrcode.QRCode(
        version=None,
        error_correction=CORRECTIONS[correction],
        box_size=1,
        border=4,
    )
    code.add_data(text)
    try:
        code.make(fit=True)
    except (DataOverflowError, ValueError):
        raise ValueError(
            "Ce texte est trop long pour le niveau de correction choisi. "
            "Réduis le texte ou choisis un niveau de correction plus faible."
        ) from None
    return code


def svg_text(code: qrcode.QRCode, size: int = 512) -> str:
    if size not in EXPORT_SIZES:
        raise ValueError("Taille d'export invalide.")
    image = code.make_image(image_factory=SvgPathFillImage)
    root = ElementTree.fromstring(image.to_string(encoding="unicode"))
    root.set("width", str(size))
    root.set("height", str(size))
    root.set("shape-rendering", "crispEdges")
    return ElementTree.tostring(root, encoding="unicode")


def png_bytes(code: qrcode.QRCode, size: int) -> bytes:
    if size not in EXPORT_SIZES:
        raise ValueError("Taille d'export invalide.")
    original = code.make_image(fill_color="black", back_color="white").get_image()
    # Integer module widths keep every square sharp; white padding makes the
    # exported canvas exactly the requested size without resampling the grid.
    scale = size // original.width
    image = original.resize(
        (original.width * scale, original.height * scale), Image.Resampling.NEAREST
    )
    canvas = Image.new("1", (size, size), 1)
    offset = (size - image.width) // 2
    canvas.paste(image, (offset, offset))
    buffer = BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def read_server_file(path: str) -> bytes:
    if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
        raise ValueError("Sélectionne un fichier texte sur le serveur.")
    try:
        # Do not block on a pipe/device or read an unbounded server file.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Sélectionne un fichier texte, pas un dossier ou un périphérique.")
            if info.st_size > MAX_FILE_BYTES:
                raise ValueError("Ce fichier est trop volumineux pour un seul QR code.")
            return source.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise ValueError("Impossible de lire ce fichier sur le serveur. Vérifie son chemin et ses droits.") from None


def decode_text_file(content: bytes) -> str:
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("Ce fichier est trop volumineux pour un seul QR code.")
    encoding = "utf-8-sig"
    if content.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encoding = "utf-32"
    elif content.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    try:
        text = content.decode(encoding)
    except UnicodeError:
        raise ValueError("Ce fichier n’est pas un texte Unicode valide. Enregistre-le en UTF-8 puis réessaie.") from None
    if not text:
        raise ValueError("Ce fichier est vide.")
    if any((ord(character) < 32 and character not in "\t\r\n") or 127 <= ord(character) <= 159 for character in text):
        raise ValueError("Ce fichier contient des données binaires. Sélectionne un fichier texte.")
    return text


def generated_payload(code: qrcode.QRCode) -> dict:
    return {
        "ok": True,
        "svg": svg_text(code),
        "png": {str(size): base64.b64encode(png_bytes(code, size)).decode("ascii") for size in EXPORT_SIZES},
        "version": code.version,
        "modules": code.modules_count,
    }


@qr_code_bp.before_request
def limit_request_size():
    request.max_content_length = 64 * 1024


@qr_code_bp.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@qr_code_bp.errorhandler(RequestEntityTooLarge)
def oversized_request(_error):
    return jsonify(ok=False, error="Ce texte est trop long pour un seul QR code."), 413


@qr_code_bp.get("")
@qr_code_bp.get("/")
def home():
    return render_template("qr_code.html", export_sizes=EXPORT_SIZES, max_file_bytes=MAX_FILE_BYTES)


@qr_code_bp.post("/generate")
def generate():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(ok=False, error="Saisis un texte à encoder."), 400
    try:
        code = build_qr(payload.get("text"), payload.get("correction", "M"))
    except ValueError as error:
        return jsonify(ok=False, error=str(error)), 400
    return jsonify(generated_payload(code))


@qr_code_bp.post("/import")
def import_file():
    try:
        if request.is_json:
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                raise ValueError("Sélectionne un fichier texte sur le serveur.")
            content = read_server_file(payload.get("path"))
            correction = payload.get("correction", "M")
        else:
            upload = request.files.get("file")
            if upload is None:
                raise ValueError("Sélectionne un fichier texte sur ton appareil.")
            content = upload.read(MAX_FILE_BYTES + 1)
            correction = request.form.get("correction", "M")
        text = decode_text_file(content)
        code = build_qr(text, correction)
    except ValueError as error:
        return jsonify(ok=False, error=str(error)), 400
    return jsonify(**generated_payload(code), text=text)
