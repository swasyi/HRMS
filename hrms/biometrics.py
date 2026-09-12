"""
HRMS Biometric Engine — Resilient Capture, Multi-Detector Fallback, Safe 1:N.

Key features:
1. In-memory image processing — zero disk I/O (no temp JPEG writes).
2. Multi-detector fallback — opencv → ssd → enforce_detection=False.
3. Vectorised 1:N matching — single NumPy matrix op, skips dim mismatches.
4. Accepts Django UploadedFile, raw bytes, BytesIO, or base64 strings.
5. Model warm-up on import — eliminates cold-start lag on first punch.
"""

import base64
import io
import json
import logging
import re
import threading

import numpy as np
from PIL import Image, ImageOps
from deepface import DeepFace

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────
DETECTOR_BACKEND = "opencv"      # ~40ms on CPU
MODEL_NAME = "Facenet512"        # 512-D high-precision embedding
EMBEDDING_DIM = 512              # Expected vector length
MAX_IMAGE_DIM = 600              # Downscale cap for fast inference
COSINE_THRESHOLD = 0.55          # Calibrated for Facenet512


# ─────────────────────────────────────────────────────────────
# Model Warm-Up (runs once in background on import)
# ─────────────────────────────────────────────────────────────
def _warm_up_model():
    """Pre-load Facenet512 weights into memory with a dummy inference."""
    try:
        dummy = np.zeros((10, 10, 3), dtype=np.uint8)
        DeepFace.represent(
            img_path=dummy,
            model_name=MODEL_NAME,
            detector_backend=DETECTOR_BACKEND,
            enforce_detection=False,
            align=False,
        )
        logger.info("[Biometrics] Model warm-up complete — %s ready.", MODEL_NAME)
    except Exception as exc:
        logger.warning("[Biometrics] Model warm-up failed (non-fatal): %s", exc)

# Fire-and-forget warm-up — won't block Django startup
threading.Thread(target=_warm_up_model, daemon=True, name="bio-warmup").start()


# ─────────────────────────────────────────────────────────────
# Image Pre-processing (Zero Disk I/O)
# ─────────────────────────────────────────────────────────────
# Regex to strip the data URI prefix from a base64 string
_B64_PREFIX_RE = re.compile(r'^data:image/[^;]+;base64,', re.IGNORECASE)


def _to_rgb_array(image_source):
    """
    Convert any image source to an RGB NumPy array.

    Accepts:
      - Django UploadedFile / InMemoryUploadedFile (has .read())
      - Raw bytes / bytearray
      - io.BytesIO
      - Base64-encoded string (with or without data URI prefix)
    """
    # ── Handle base64 strings ──
    if isinstance(image_source, str):
        # Strip "data:image/jpeg;base64," prefix if present
        cleaned = _B64_PREFIX_RE.sub('', image_source)
        try:
            raw_bytes = base64.b64decode(cleaned)
        except Exception as exc:
            raise ValueError(f"Invalid base64 image data: {exc}")
        buf = io.BytesIO(raw_bytes)

    # ── Handle raw bytes ──
    elif isinstance(image_source, (bytes, bytearray)):
        buf = io.BytesIO(image_source)

    # ── Handle file-like objects (Django UploadedFile, BytesIO) ──
    elif hasattr(image_source, 'read'):
        try:
            image_source.seek(0)
        except Exception:
            pass
        buf = io.BytesIO(image_source.read())

    else:
        buf = io.BytesIO(image_source)

    pil_img = Image.open(buf)

    # Auto-fix mobile EXIF rotation
    try:
        pil_img = ImageOps.exif_transpose(pil_img)
    except Exception:
        pass

    pil_img = pil_img.convert("RGB")

    # Downscale for fast CPU inference (max 600px on longest side)
    if max(pil_img.size) > MAX_IMAGE_DIM:
        pil_img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), Image.Resampling.BILINEAR)

    return np.array(pil_img)


# ─────────────────────────────────────────────────────────────
# Face Embedding Extraction — Multi-Detector Fallback
# ─────────────────────────────────────────────────────────────
def extract_face_encoding(image_source):
    """
    Extract a 512-D Facenet512 embedding from an image.

    Returns:
      (list[float], None)  — on success
      (None, str)          — on failure, with a user-friendly error message

    Never raises unhandled exceptions.
    """
    try:
        rgb_array = _to_rgb_array(image_source)
    except Exception as exc:
        logger.error("[Biometrics] Image pre-processing failed: %s", exc)
        return None, "Could not read the image. Please try a different photo."

    # ── Pass 1: Strict detection with opencv (fastest) ──
    try:
        results = DeepFace.represent(
            img_path=rgb_array,
            model_name=MODEL_NAME,
            detector_backend="opencv",
            enforce_detection=True,
            align=True,
        )
        if results and len(results) > 0:
            return results[0]["embedding"], None
    except Exception:
        pass

    # ── Pass 2: Try SSD detector (better with varied angles) ──
    try:
        results = DeepFace.represent(
            img_path=rgb_array,
            model_name=MODEL_NAME,
            detector_backend="ssd",
            enforce_detection=True,
            align=True,
        )
        if results and len(results) > 0:
            return results[0]["embedding"], None
    except Exception:
        pass

    # ── Pass 3: Relaxed detection (handles unusual lighting / angles) ──
    try:
        results = DeepFace.represent(
            img_path=rgb_array,
            model_name=MODEL_NAME,
            detector_backend="opencv",
            enforce_detection=False,
            align=True,
        )
        if results and len(results) > 0:
            logger.info("[Biometrics] Face detected via fallback (enforce_detection=False).")
            return results[0]["embedding"], None
    except Exception as exc:
        logger.error("[Biometrics] All detection passes failed: %s", exc)

    return None, (
        "No face detected. Please ensure good lighting, "
        "face the camera directly, and avoid tilting your head."
    )
# biometrics.py

def _to_rgb_array(image_source):
    """
    Convert any image source to an RGB NumPy array safely.
    Handles Django UploadedFile, raw bytes, BytesIO, base64 strings, and paths.
    """
    if isinstance(image_source, str):
        cleaned = _B64_PREFIX_RE.sub('', image_source)
        try:
            raw_bytes = base64.b64decode(cleaned)
        except Exception as exc:
            raise ValueError(f"Invalid base64 image data: {exc}")
        buf = io.BytesIO(raw_bytes)

    elif isinstance(image_source, (bytes, bytearray)):
        buf = io.BytesIO(image_source)

    elif hasattr(image_source, 'read'):
        # CRUCIAL: Always seek to byte 0 before and after reading
        try:
            image_source.seek(0)
        except Exception:
            pass
        content = image_source.read()
        try:
            image_source.seek(0)
        except Exception:
            pass
        buf = io.BytesIO(content)

    elif isinstance(image_source, io.BytesIO):
        image_source.seek(0)
        buf = io.BytesIO(image_source.read())
        image_source.seek(0)

    else:
        buf = io.BytesIO(image_source)

    # Open image with PIL
    buf.seek(0)
    pil_img = Image.open(buf)

    # Fix EXIF orientation (especially for mobile/WhatsApp photos)
    try:
        pil_img = ImageOps.exif_transpose(pil_img)
    except Exception:
        pass

    pil_img = pil_img.convert("RGB")

    # Downscale if excessively large, but do not make it too small
    if max(pil_img.size) > 1200:
        pil_img.thumbnail((1200, 1200), Image.Resampling.BILINEAR)

    # Return as contiguous uint8 array
    arr = np.ascontiguousarray(np.array(pil_img), dtype=np.uint8)
    return arr


def extract_face_encoding(image_source):
    """
    Extract a 512-D Facenet512 embedding from an image.
    Never fails on standard profile images.
    """
    try:
        rgb_array = _to_rgb_array(image_source)
    except Exception as exc:
        logger.error("[Biometrics] Image pre-processing failed: %s", exc)
        return None, "Could not read the image. Please try a different photo."

    # ── Pass 1: Strict OpenCV detector with alignment ──
    try:
        results = DeepFace.represent(
            img_path=rgb_array,
            model_name=MODEL_NAME,
            detector_backend="opencv",
            enforce_detection=True,
            align=True,
        )
        if results and len(results) > 0 and "embedding" in results[0]:
            return results[0]["embedding"], None
    except Exception as e:
        logger.debug("[Biometrics] Pass 1 (opencv strict) skipped: %s", e)

    # ── Pass 2: SSD detector (superior for WhatsApp/mobile pictures) ──
    try:
        results = DeepFace.represent(
            img_path=rgb_array,
            model_name=MODEL_NAME,
            detector_backend="ssd",
            enforce_detection=True,
            align=True,
        )
        if results and len(results) > 0 and "embedding" in results[0]:
            return results[0]["embedding"], None
    except Exception as e:
        logger.debug("[Biometrics] Pass 2 (ssd strict) skipped: %s", e)

    # ── Pass 3: Relaxed Detection WITHOUT forced alignment ──
    # Setting align=False is essential: it avoids crashing when landmarks are fuzzy
    try:
        results = DeepFace.represent(
            img_path=rgb_array,
            model_name=MODEL_NAME,
            detector_backend="opencv",
            enforce_detection=False,
            align=False,
        )
        if results and len(results) > 0 and "embedding" in results[0]:
            logger.info("[Biometrics] Face detected via relaxed Pass 3 fallback.")
            return results[0]["embedding"], None
    except Exception as e:
        logger.error("[Biometrics] Pass 3 relaxed representation failed: %s", e)

    # ── Pass 4: Direct full-frame representation ──
    # Uses the entire crop directly (guaranteed to generate the Facenet vector)
    try:
        results = DeepFace.represent(
            img_path=rgb_array,
            model_name=MODEL_NAME,
            detector_backend="skip",
            enforce_detection=False,
            align=False,
        )
        if results and len(results) > 0 and "embedding" in results[0]:
            logger.info("[Biometrics] Extracted embedding using skip-detection mode.")
            return results[0]["embedding"], None
    except Exception as exc:
        logger.error("[Biometrics] All extraction attempts failed: %s", exc)

    return None, "No face detected. Please ensure good lighting and upload a clear picture."
# ─────────────────────────────────────────────────────────────
# Safe Encoding Parser
# ─────────────────────────────────────────────────────────────
def _parse_encoding(raw_encoding):
    """
    Safely parse a stored face_encoding into a float64 NumPy array.

    Handles:
      - list[float]  (normal JSONField storage)
      - str          (JSON-serialised list, or repr string)
      - np.ndarray   (already parsed)

    Returns np.ndarray or None on failure.
    """
    if raw_encoding is None:
        return None

    if isinstance(raw_encoding, np.ndarray):
        return raw_encoding.astype(np.float64)

    if isinstance(raw_encoding, str):
        try:
            raw_encoding = json.loads(raw_encoding)
        except (json.JSONDecodeError, ValueError):
            return None

    if isinstance(raw_encoding, (list, tuple)):
        try:
            return np.array(raw_encoding, dtype=np.float64)
        except (ValueError, TypeError):
            return None

    return None


# ─────────────────────────────────────────────────────────────
# 1:1 Verification (Mobile Punch)
# ─────────────────────────────────────────────────────────────
def verify_1_to_1(image_source, reference_encoding, threshold=0.58):
    """
    Cosine-distance verification between a live image and a stored embedding.
    Returns (is_match: bool, message: str).
    """
    encoding, err = extract_face_encoding(image_source)
    if encoding is None:
        return False, err or "No face detected in photo. Please look straight into the camera."

    known = _parse_encoding(reference_encoding)
    if known is None:
        return False, "Stored biometric data is corrupted."

    live = np.array(encoding, dtype=np.float64)

    # Dimension mismatch guard
    if known.shape != live.shape:
        logger.warning(
            "[Biometrics] Dimension mismatch: stored=%s, live=%s",
            known.shape, live.shape
        )
        return False, "Biometric vector dimension mismatch."

    norm_k = np.linalg.norm(known)
    norm_l = np.linalg.norm(live)
    if norm_k == 0 or norm_l == 0:
        return False, "Invalid facial vector (zero norm)."

    cosine_dist = 1.0 - (np.dot(known, live) / (norm_k * norm_l))
    is_match = bool(cosine_dist <= threshold)
    return is_match, f"Distance: {cosine_dist:.4f}"


# ─────────────────────────────────────────────────────────────
# 1:N Matching — Vectorised (Kiosk / Enrollment Duplicate Check)
# ─────────────────────────────────────────────────────────────
def match_1_to_n(image_source, all_biometrics, threshold=None):
    """
    Vectorised cosine-distance search across all enrolled employees.

    Instead of looping in Python, this stacks all known embeddings into
    a (N, D) matrix and computes all distances in a single NumPy
    dot-product + norm operation.

    Gracefully skips records with corrupted or dimension-mismatched encodings
    instead of crashing with a shape broadcast error.

    Returns (matched_employee | None, message: str).
    """
    if threshold is None:
        threshold = COSINE_THRESHOLD

    encoding, err = extract_face_encoding(image_source)
    if encoding is None:
        return None, err or "No face detected."

    live = np.array(encoding, dtype=np.float64)
    live_dim = live.shape[0]
    norm_live = np.linalg.norm(live)
    if norm_live == 0:
        return None, "Invalid facial vector."

    # Materialise queryset into lists for indexing
    bio_list = list(all_biometrics)
    if not bio_list:
        return None, "No enrolled employees found."

    # Build the matrix row by row, skipping bad records
    valid_bios = []
    rows = []
    for b in bio_list:
        vec = _parse_encoding(b.face_encoding)
        if vec is None:
            logger.warning("[Biometrics] Skipping employee %s — unparseable encoding.", b.employee_id)
            continue
        if vec.shape[0] != live_dim:
            logger.warning(
                "[Biometrics] Skipping employee %s — dimension mismatch (stored=%d, live=%d).",
                b.employee_id, vec.shape[0], live_dim
            )
            continue
        rows.append(vec)
        valid_bios.append(b)

    if not rows:
        return None, "No valid enrolled embeddings found."

    # Stack into (N, D) matrix
    matrix = np.array(rows, dtype=np.float64)
    norms = np.linalg.norm(matrix, axis=1)

    # Guard against zero-norm rows
    valid_mask = norms > 0
    if not np.any(valid_mask):
        return None, "All stored embeddings are invalid."

    # Vectorised cosine distances
    dots = matrix[valid_mask] @ live
    cosine_dists = 1.0 - (dots / (norms[valid_mask] * norm_live))

    # Find best match below threshold
    min_idx = np.argmin(cosine_dists)
    min_dist = cosine_dists[min_idx]

    # Map back through both masks to the original bio
    valid_norm_indices = np.where(valid_mask)[0]
    matched_bio = valid_bios[valid_norm_indices[min_idx]]

    # Terminal output to observe the exact distance
    print(
        f"\n[DEBUG] Best Match: {matched_bio.employee.full_name} | "
        f"Distance: {min_dist:.4f} | Allowed Threshold: {threshold}"
    )

    if min_dist <= threshold:
        return matched_bio.employee, f"Matched {matched_bio.employee.full_name} (dist: {min_dist:.4f})"

    return None, "Face not recognized in employee records."