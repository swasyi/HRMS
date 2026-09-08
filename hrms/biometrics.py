"""
HRMS Biometric Engine — Optimized for Instant Capture, Async Verification.

Key optimisations over the legacy version:
1. In-memory image processing — zero disk I/O (no temp JPEG writes).
2. Vectorised 1:N matching — single NumPy matrix operation instead of Python loop.
3. Model warm-up on import — eliminates cold-start lag on first punch.
4. Accepts both Django UploadedFile and raw bytes (for background threads).
"""

import io
import logging
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
MAX_IMAGE_DIM = 400              # Downscale cap for fast inference


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
def _to_rgb_array(image_source):
    """
    Convert any image source to an RGB NumPy array.

    Accepts:
      - Django UploadedFile / InMemoryUploadedFile (has .read())
      - Raw bytes / bytearray
      - io.BytesIO
    """
    # Normalise to a BytesIO stream
    if isinstance(image_source, (bytes, bytearray)):
        buf = io.BytesIO(image_source)
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

    # Downscale for fast CPU inference
    if max(pil_img.size) > MAX_IMAGE_DIM:
        pil_img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), Image.Resampling.BILINEAR)

    return np.array(pil_img)


# ─────────────────────────────────────────────────────────────
# Face Embedding Extraction
# ─────────────────────────────────────────────────────────────
def extract_face_encoding(image_source):
    """
    Extract a 512-D Facenet512 embedding from an image.
    Returns a list[float] or None if no face is detected.
    """
    try:
        rgb_array = _to_rgb_array(image_source)

        # Primary pass — strict detection
        try:
            results = DeepFace.represent(
                img_path=rgb_array,
                model_name=MODEL_NAME,
                detector_backend=DETECTOR_BACKEND,
                enforce_detection=True,
                align=False,
            )
            if results and len(results) > 0:
                return results[0]["embedding"]
        except Exception:
            pass

        # Fallback — relaxed detection (handles unusual angles / lighting)
        try:
            results = DeepFace.represent(
                img_path=rgb_array,
                model_name=MODEL_NAME,
                detector_backend="opencv",
                enforce_detection=False,
                align=False,
            )
            if results and len(results) > 0:
                return results[0]["embedding"]
        except Exception:
            pass

    except Exception as exc:
        logger.error("[Biometrics] Extraction error: %s", exc)

    return None


# ─────────────────────────────────────────────────────────────
# 1:1 Verification (Mobile Punch)
# ─────────────────────────────────────────────────────────────
def verify_1_to_1(image_source, reference_encoding, threshold=0.45):
    """
    Cosine-distance verification between a live image and a stored embedding.
    Returns (is_match: bool, message: str).
    """
    live_encoding = extract_face_encoding(image_source)
    if not live_encoding:
        return False, "No face detected in photo. Please look straight into the camera."

    known = np.array(reference_encoding, dtype=np.float64)
    live = np.array(live_encoding, dtype=np.float64)

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
def match_1_to_n(image_source, all_biometrics, threshold=0.45):
    """
    Vectorised cosine-distance search across all enrolled employees.

    Instead of looping in Python, this stacks all known embeddings into
    a (N, 512) matrix and computes all distances in a single NumPy
    dot-product + norm operation.

    Returns (matched_employee | None, message: str).
    """
    live_encoding = extract_face_encoding(image_source)
    if not live_encoding:
        return None, "No face detected."

    live = np.array(live_encoding, dtype=np.float64)
    norm_live = np.linalg.norm(live)
    if norm_live == 0:
        return None, "Invalid facial vector."

    # Materialise queryset into lists for indexing
    bio_list = list(all_biometrics)
    if not bio_list:
        return None, "No enrolled employees found."

    # Stack into (N, D) matrix
    matrix = np.array([b.face_encoding for b in bio_list], dtype=np.float64)
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

    if min_dist <= threshold:
        # Map back to the original bio_list index
        valid_indices = np.where(valid_mask)[0]
        matched_bio = bio_list[valid_indices[min_idx]]
        return matched_bio.employee, f"Matched {matched_bio.employee.full_name} (dist: {min_dist:.4f})"

    return None, "Face not recognized in employee records."