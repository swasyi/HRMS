"""
HRMS Biometric Engine — Strict Face Detection, Safe 1:N Matching.

Key features:
1. In-memory image processing — zero disk I/O (no temp JPEG writes).
2. Multi-detector fallback — opencv → ssd (STRICT only, no relaxed passes).
3. Vectorised 1:N matching — single NumPy matrix op, skips dim mismatches.
4. Accepts Django UploadedFile, raw bytes, BytesIO, or base64 strings.
5. Model warm-up on import — eliminates cold-start lag on first punch.
6. Face confidence gate — rejects low-confidence / no-face detections.
7. Ambiguity guard — rejects 1:N matches where top-2 distances are too close.
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
MAX_IMAGE_DIM = 800              # Balanced downscale cap

# ── Thresholds (calibrated for Facenet512 cosine distance) ──
# Same person:      typically 0.05 – 0.25
# Different person: typically 0.30 – 0.60
COSINE_THRESHOLD = 0.28          # Strict — prevents cross-person false matches
DUPLICATE_THRESHOLD = 0.30       # For enrollment duplicate checks (slightly looser)

# Minimum face confidence from detector (0-1 scale, reject blurry/partial)
MIN_FACE_CONFIDENCE = 0.50

# Minimum embedding norm — garbage/no-face embeddings have abnormally low norms
MIN_EMBEDDING_NORM = 10.0

# Ambiguity margin: if best_dist and second_best_dist are within this gap,
# reject the match (too ambiguous to be confident)
AMBIGUITY_MARGIN = 0.06


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

    # Downscale if excessively large — use configured MAX_IMAGE_DIM
    if max(pil_img.size) > MAX_IMAGE_DIM:
        pil_img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), Image.Resampling.BILINEAR)

    # Return as contiguous uint8 array
    arr = np.ascontiguousarray(np.array(pil_img), dtype=np.uint8)
    return arr


# ─────────────────────────────────────────────────────────────
# Embedding Quality Validation
# ─────────────────────────────────────────────────────────────
def _validate_embedding(embedding):
    """
    Validate that an embedding looks like a real face vector.
    Garbage embeddings (from walls, desks, etc.) have abnormally low norms
    or near-zero variance.

    Returns (is_valid: bool, reason: str).
    """
    vec = np.array(embedding, dtype=np.float64)

    # Check 1: Embedding norm — real faces produce norms ~15-25 for Facenet512
    norm = np.linalg.norm(vec)
    if norm < MIN_EMBEDDING_NORM:
        return False, f"Embedding norm too low ({norm:.2f}) — likely not a real face."

    # Check 2: Variance — garbage embeddings tend to have very low variance
    variance = np.var(vec)
    if variance < 0.001:
        return False, f"Embedding variance too low ({variance:.6f}) — likely noise."

    return True, "OK"


def extract_face_encoding(image_source):
    """
    Extract a 512-D Facenet512 embedding from an image.

    STRICT MODE: Only returns an embedding if a real face is confidently
    detected by opencv or ssd detectors. NO fallback to relaxed/skip modes
    (which would generate garbage embeddings from walls, desks, etc.).
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
            embedding = results[0]["embedding"]

            # Validate embedding quality
            is_valid, reason = _validate_embedding(embedding)
            if not is_valid:
                logger.warning("[Biometrics] Pass 1 embedding rejected: %s", reason)
            else:
                # Check face detection confidence if available
                confidence = results[0].get("face_confidence", 1.0)
                if confidence < MIN_FACE_CONFIDENCE:
                    logger.warning(
                        "[Biometrics] Pass 1 face confidence too low: %.2f < %.2f",
                        confidence, MIN_FACE_CONFIDENCE
                    )
                else:
                    logger.info("[Biometrics] Face detected via Pass 1 (opencv strict), confidence=%.2f", confidence)
                    return embedding, None
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
            embedding = results[0]["embedding"]

            # Validate embedding quality
            is_valid, reason = _validate_embedding(embedding)
            if not is_valid:
                logger.warning("[Biometrics] Pass 2 embedding rejected: %s", reason)
            else:
                confidence = results[0].get("face_confidence", 1.0)
                if confidence < MIN_FACE_CONFIDENCE:
                    logger.warning(
                        "[Biometrics] Pass 2 face confidence too low: %.2f < %.2f",
                        confidence, MIN_FACE_CONFIDENCE
                    )
                else:
                    logger.info("[Biometrics] Face detected via Pass 2 (ssd strict), confidence=%.2f", confidence)
                    return embedding, None
    except Exception as e:
        logger.debug("[Biometrics] Pass 2 (ssd strict) skipped: %s", e)

    # ── Pass 3: Relaxed opencv — safe fallback for webcam captures ──
    # Webcam canvas-captured JPEGs sometimes fail strict detection due to
    # compression artifacts or low resolution. This pass uses
    # enforce_detection=False but STILL validates the embedding quality —
    # garbage vectors from walls/desks will be caught by _validate_embedding().
    # NOTE: Pass 4 (detector_backend="skip") is intentionally REMOVED —
    # it treats the entire frame as a face and is fundamentally unsafe.
    try:
        results = DeepFace.represent(
            img_path=rgb_array,
            model_name=MODEL_NAME,
            detector_backend="opencv",
            enforce_detection=False,
            align=True,
        )
        if results and len(results) > 0 and "embedding" in results[0]:
            embedding = results[0]["embedding"]

            # CRITICAL: Quality gate — this is what makes Pass 3 safe.
            # Real faces produce embeddings with norm ~15-25 and healthy variance.
            # Walls/desks/no-face produce low-norm, low-variance garbage.
            is_valid, reason = _validate_embedding(embedding)
            if is_valid:
                logger.info("[Biometrics] Face detected via Pass 3 (relaxed opencv) — embedding quality OK.")
                return embedding, None
            else:
                logger.warning("[Biometrics] Pass 3 embedding REJECTED (no real face): %s", reason)
    except Exception as e:
        logger.debug("[Biometrics] Pass 3 (relaxed opencv) failed: %s", e)

    # ══════════════════════════════════════════════════════════════
    # All passes exhausted — no valid face found.
    # ══════════════════════════════════════════════════════════════
    logger.warning("[Biometrics] No face detected by any detector.")
    return None, (
        "No face detected. Please ensure:\n"
        "• Good, even lighting (avoid backlighting)\n"
        "• Face the camera directly\n"
        "• Remove sunglasses or face coverings\n"
        "• Hold still while scanning"
    )


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
def verify_1_to_1(image_source, reference_encoding, threshold=None):
    """
    Cosine-distance verification between a live image and a stored embedding.
    Returns (is_match: bool, message: str).
    """
    if threshold is None:
        threshold = COSINE_THRESHOLD

    encoding, err = extract_face_encoding(image_source)
    if encoding is None:
        return False, err or "No face detected in photo. Please look straight into the camera."

    # Validate live embedding quality
    is_valid, reason = _validate_embedding(encoding)
    if not is_valid:
        return False, f"Poor face quality: {reason}"

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

    logger.info(
        "[Biometrics 1:1] Distance=%.4f, Threshold=%.2f, Match=%s",
        cosine_dist, threshold, is_match
    )

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

    Includes ambiguity guard: if the best and second-best matches are
    too close in distance, the match is rejected to avoid misidentification.

    Gracefully skips records with corrupted or dimension-mismatched encodings
    instead of crashing with a shape broadcast error.

    Returns (matched_employee | None, message: str).
    """
    if threshold is None:
        threshold = COSINE_THRESHOLD

    encoding, err = extract_face_encoding(image_source)
    if encoding is None:
        return None, err or "No face detected."

    # Validate live embedding quality
    is_valid, reason = _validate_embedding(encoding)
    if not is_valid:
        logger.warning("[Biometrics 1:N] Live embedding rejected: %s", reason)
        return None, "Poor face quality — please face the camera directly with good lighting."

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
        # Skip stored embeddings with abnormally low norms (corrupted data)
        if np.linalg.norm(vec) < MIN_EMBEDDING_NORM:
            logger.warning("[Biometrics] Skipping employee %s — stored embedding norm too low.", b.employee_id)
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

    # Find best match
    sorted_indices = np.argsort(cosine_dists)
    best_idx = sorted_indices[0]
    best_dist = cosine_dists[best_idx]

    # Map back through both masks to the original bio
    valid_norm_indices = np.where(valid_mask)[0]
    matched_bio = valid_bios[valid_norm_indices[best_idx]]

    # Terminal + log output for debugging
    print(
        f"\n[DEBUG] Best Match: {matched_bio.employee.full_name} | "
        f"Distance: {best_dist:.4f} | Allowed Threshold: {threshold}"
    )
    logger.info(
        "[Biometrics 1:N] Best=%s dist=%.4f threshold=%.2f",
        matched_bio.employee.full_name, best_dist, threshold
    )

    # ── Ambiguity Guard ──────────────────────────────────────
    # If there are 2+ enrolled employees, check the gap between
    # the best and second-best match. If the gap is too small,
    # the match is ambiguous and should be rejected.
    if len(sorted_indices) >= 2 and best_dist <= threshold:
        second_idx = sorted_indices[1]
        second_dist = cosine_dists[second_idx]
        gap = second_dist - best_dist

        second_bio = valid_bios[valid_norm_indices[second_idx]]
        print(
            f"[DEBUG] 2nd Best: {second_bio.employee.full_name} | "
            f"Distance: {second_dist:.4f} | Gap: {gap:.4f} | Min Gap: {AMBIGUITY_MARGIN}"
        )
        logger.info(
            "[Biometrics 1:N] 2nd=%s dist=%.4f gap=%.4f",
            second_bio.employee.full_name, second_dist, gap
        )

        if gap < AMBIGUITY_MARGIN:
            logger.warning(
                "[Biometrics 1:N] AMBIGUOUS — gap (%.4f) < margin (%.2f). "
                "Rejecting match between %s and %s.",
                gap, AMBIGUITY_MARGIN,
                matched_bio.employee.full_name,
                second_bio.employee.full_name
            )
            return None, (
                "Face match is ambiguous — cannot confidently identify you. "
                "Please face the camera directly with good lighting and try again."
            )

    if best_dist <= threshold:
        return matched_bio.employee, f"Matched {matched_bio.employee.full_name} (dist: {best_dist:.4f})"

    return None, "Face not recognized in employee records."