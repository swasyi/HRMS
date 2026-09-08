# from deepface import DeepFace
# import numpy as np
# from PIL import Image
# import tempfile
# import os
#
#
# def extract_face_encoding(image_file):
#     """Extracts a 128-d face embedding vector using Facenet via DeepFace."""
#     temp_path = None
#     try:
#         # Save temporary image file for processing
#         with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as temp_file:
#             for chunk in image_file.chunks():
#                 temp_file.write(chunk)
#             temp_path = temp_file.name
#
#         embedding_objs = DeepFace.represent(
#             img_path=temp_path,
#             model_name="Facenet",
#             enforce_detection=True
#         )
#         if embedding_objs and len(embedding_objs) > 0:
#             return embedding_objs[0]["embedding"]
#         return None
#     except Exception:
#         return None
#     finally:
#         if temp_path and os.path.exists(temp_path):
#             os.remove(temp_path)
#
#
# def verify_1_to_1(uploaded_image, reference_encoding, tolerance=0.40):
#     live_encoding = extract_face_encoding(uploaded_image)
#     if not live_encoding:
#         return False, "No face detected in live photo. Please align your face clearly."
#
#     # Cosine distance formula
#     a = np.array(reference_encoding)
#     b = np.array(live_encoding)
#     cosine_distance = 1 - (np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
#
#     is_match = cosine_distance <= tolerance
#     return bool(is_match), f"Distance: {cosine_distance:.2f}"
#
#
# def match_1_to_n(uploaded_image, all_biometrics, tolerance=0.40):
#     live_encoding = extract_face_encoding(uploaded_image)
#     if not live_encoding:
#         return None, "No face detected."
#
#     b = np.array(live_encoding)
#     norm_b = np.linalg.norm(b)
#
#     for biometric in all_biometrics:
#         a = np.array(biometric.face_encoding)
#         cosine_distance = 1 - (np.dot(a, b) / (np.linalg.norm(a) * norm_b))
#         if cosine_distance <= tolerance:
#             return biometric.employee, f"Matched {biometric.employee.full_name}"
#
#     return None, "Face not recognized in employee records."


# import numpy as np
# from PIL import Image
# import io
#
# # If you installed face_recognition:
# try:
#     import face_recognition
#
#     USE_FACE_REC = True
# except ImportError:
#     USE_FACE_REC = False
#
# # If you installed deepface:
# try:
#     from deepface import DeepFace
#
#     USE_DEEPFACE = True
# except ImportError:
#     USE_DEEPFACE = False
#
#
# def _load_image_as_rgb_array(image_file):
#     """
#     Safely converts Django UploadedFile / InMemoryUploadedFile / File to an RGB NumPy array.
#     """
#     try:
#         image_file.seek(0)
#     except Exception:
#         pass
#
#     # Read image through Pillow
#     pil_img = Image.open(io.BytesIO(image_file.read()))
#
#     # Auto-rotate according to EXIF orientation (common issue with phone cameras)
#     try:
#         from PIL import ImageOps
#         pil_img = ImageOps.exif_transpose(pil_img)
#     except Exception:
#         pass
#
#     # Ensure 3-channel RGB
#     rgb_img = pil_img.convert('RGB')
#     return np.array(rgb_img)
#
#
# def extract_face_encoding(image_file):
#     """
#     Extracts a 128-dimensional face embedding vector.
#     """
#     try:
#         rgb_array = _load_image_as_rgb_array(image_file)
#
#         if USE_FACE_REC:
#             # 1. Try default HOG detector
#             encodings = face_recognition.face_encodings(rgb_array)
#
#             # 2. If not detected on the first pass, try upsampling
#             if not encodings:
#                 face_locations = face_recognition.face_locations(rgb_array, number_of_times_to_upsample=2)
#                 encodings = face_recognition.face_encodings(rgb_array, known_face_locations=face_locations)
#
#             if encodings:
#                 return encodings[0].tolist()
#
#         elif USE_DEEPFACE:
#             # Using DeepFace Facenet
#             res = DeepFace.represent(
#                 img_path=rgb_array,
#                 model_name="Facenet",
#                 detector_backend="opencv",
#                 enforce_detection=False
#             )
#             if res and len(res) > 0:
#                 return res[0]["embedding"]
#
#     except Exception as e:
#         print(f"[Biometric Error]: {e}")
#         return None
#
#     return None
#
#
# def verify_1_to_1(uploaded_image, reference_encoding, tolerance=0.48):
#     live_encoding = extract_face_encoding(uploaded_image)
#     if not live_encoding:
#         return False, "No face detected in live photo. Please look directly at the camera."
#
#     known_arr = np.array(reference_encoding)
#     live_arr = np.array(live_encoding)
#
#     if USE_FACE_REC:
#         distance = face_recognition.face_distance([known_arr], live_arr)[0]
#     else:
#         # Cosine distance for DeepFace
#         distance = 1 - (np.dot(known_arr, live_arr) / (np.linalg.norm(known_arr) * np.linalg.norm(live_arr)))
#
#     return bool(distance <= tolerance), f"Match score: {distance:.2f}"
#
#
# def match_1_to_n(uploaded_image, all_biometrics, tolerance=0.48):
#     live_encoding = extract_face_encoding(uploaded_image)
#     if not live_encoding:
#         return None, "No face detected."
#
#     live_arr = np.array(live_encoding)
#
#     for biometric in all_biometrics:
#         known_arr = np.array(biometric.face_encoding)
#         if USE_FACE_REC:
#             dist = face_recognition.face_distance([known_arr], live_arr)[0]
#         else:
#             dist = 1 - (np.dot(known_arr, live_arr) / (np.linalg.norm(known_arr) * np.linalg.norm(live_arr)))
#
#         if dist <= tolerance:
#             return biometric.employee, f"Matched {biometric.employee.full_name}"
#
#     return None, "Face not recognized in employee records."


# import numpy as np
# from PIL import Image, ImageOps
# import io
# import cv2
#
# # Try importing face_recognition (dlib based)
# try:
#     import face_recognition
#
#     USE_FACE_REC = True
# except ImportError:
#     USE_FACE_REC = False
#
#
# def _get_clean_rgb_image(image_file):
#     """
#     Reads any uploaded image file, corrects orientation,
#     resizes to an optimal resolution, and returns RGB numpy array.
#     """
#     try:
#         image_file.seek(0)
#     except Exception:
#         pass
#
#     # Open with PIL
#     pil_image = Image.open(io.BytesIO(image_file.read()))
#
#     # Correct EXIF rotation (fixes sideways webcam/phone shots)
#     try:
#         pil_image = ImageOps.exif_transpose(pil_image)
#     except Exception:
#         pass
#
#     # Ensure standard RGB format
#     pil_image = pil_image.convert('RGB')
#
#     # Resize if image is too large (webcam images > 1280px often fail in dlib HOG)
#     max_dimension = 1000
#     if max(pil_image.size) > max_dimension:
#         pil_image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
#
#     return np.array(pil_image)
#
#
# def extract_face_encoding(image_file):
#     """
#     Extracts 128-dimensional face embedding vector using multi-pass detection.
#     """
#     try:
#         rgb_image = _get_clean_rgb_image(image_file)
#
#         if USE_FACE_REC:
#             # Pass 1: Standard HOG detection
#             encodings = face_recognition.face_encodings(rgb_image)
#             if encodings:
#                 return encodings[0].tolist()
#
#             # Pass 2: Upsample 1x
#             locations = face_recognition.face_locations(rgb_image, number_of_times_to_upsample=1)
#             encodings = face_recognition.face_encodings(rgb_image, known_face_locations=locations)
#             if encodings:
#                 return encodings[0].tolist()
#
#             # Pass 3: OpenCV Haar Cascade detector fallback
#             gray = cv2.cvtColor(rgb_image, cv2.COLOR_RGB2GRAY)
#             haar_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
#             detected_faces = haar_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
#
#             if len(detected_faces) > 0:
#                 # Convert (x, y, w, h) -> (top, right, bottom, left) for face_recognition
#                 cv_locations = []
#                 for (x, y, w, h) in detected_faces:
#                     cv_locations.append((y, x + w, y + h, x))
#
#                 encodings = face_recognition.face_encodings(rgb_image, known_face_locations=cv_locations)
#                 if encodings:
#                     return encodings[0].tolist()
#
#     except Exception as e:
#         print(f"[Biometrics Extraction Error]: {e}")
#         return None
#
#     return None
#
#
# def verify_1_to_1(uploaded_image, reference_encoding, tolerance=0.50):
#     live_encoding = extract_face_encoding(uploaded_image)
#     if not live_encoding:
#         return False, "No face detected in live photo. Please face the camera directly."
#
#     known_arr = np.array(reference_encoding)
#     live_arr = np.array(live_encoding)
#
#     distance = face_recognition.face_distance([known_arr], live_arr)[0]
#     is_match = distance <= tolerance
#     return bool(is_match), f"Distance: {distance:.2f}"
#
#
# def match_1_to_n(uploaded_image, all_biometrics, tolerance=0.50):
#     live_encoding = extract_face_encoding(uploaded_image)
#     if not live_encoding:
#         return None, "No face detected."
#
#     live_arr = np.array(live_encoding)
#
#     for biometric in all_biometrics:
#         known_arr = np.array(biometric.face_encoding)
#         dist = face_recognition.face_distance([known_arr], live_arr)[0]
#         if dist <= tolerance:
#             return biometric.employee, f"Matched {biometric.employee.full_name}"
#
#     return None, "Face not recognized in employee records."


import numpy as np
from PIL import Image, ImageOps
import io
import tempfile
import os
from deepface import DeepFace

# Using state-of-the-art detector and recognition models
# DETECTOR_BACKEND = "retinaface"  # Fallback chain: retinaface -> mtcnn -> opencv
# DETECTOR_BACKEND = "opencv"   # Runs in ~40ms on CPU
# MODEL_NAME = "Facenet512"  # High-precision 512-dimensional embedding
#
#
# def _save_file_to_temp_jpeg(image_file):
#     """
#     Reads incoming Django file, corrects EXIF orientation,
#     converts to clean RGB, and writes to a temporary file.
#     """
#     try:
#         image_file.seek(0)
#     except Exception:
#         pass
#
#     pil_img = Image.open(io.BytesIO(image_file.read()))
#
#     # Auto-fix mobile orientation
#     try:
#         pil_img = ImageOps.exif_transpose(pil_img)
#     except Exception:
#         pass
#
#     rgb_img = pil_img.convert("RGB")
#
#     # Resize if abnormally large to speed up inference
#     max_dim = 1200
#     if max(rgb_img.size) > max_dim:
#         rgb_img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
#
#     temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
#     rgb_img.save(temp_file.name, format="JPEG", quality=95)
#     temp_file.close()
#     return temp_file.name
#
#
# def extract_face_encoding(image_file):
#     """
#     Extracts deep 512-D face embedding vector using RetinaFace + Facenet512.
#     """
#     temp_path = None
#     try:
#         temp_path = _save_file_to_temp_jpeg(image_file)
#
#         # 1. Primary Pass: RetinaFace (Deep Learning)
#         try:
#             embeddings = DeepFace.represent(
#                 img_path=temp_path,
#                 model_name=MODEL_NAME,
#                 detector_backend=DETECTOR_BACKEND,
#                 enforce_detection=True,
#                 align=True
#             )
#             if embeddings and len(embeddings) > 0:
#                 return embeddings[0]["embedding"]
#         except Exception:
#             pass
#
#         # 2. Secondary Pass: MTCNN / OpenCV detector if primary fails
#         for fallback_detector in ["mtcnn", "opencv"]:
#             try:
#                 embeddings = DeepFace.represent(
#                     img_path=temp_path,
#                     model_name=MODEL_NAME,
#                     detector_backend=fallback_detector,
#                     enforce_detection=True,
#                     align=True
#                 )
#                 if embeddings and len(embeddings) > 0:
#                     return embeddings[0]["embedding"]
#             except Exception:
#                 continue
#
#         # 3. Final Fallback: Non-enforced extraction
#         try:
#             embeddings = DeepFace.represent(
#                 img_path=temp_path,
#                 model_name=MODEL_NAME,
#                 detector_backend="opencv",
#                 enforce_detection=False,
#                 align=False
#             )
#             if embeddings and len(embeddings) > 0:
#                 return embeddings[0]["embedding"]
#         except Exception:
#             pass
#
#     except Exception as e:
#         print(f"[Biometric Error]: {e}")
#         return None
#     finally:
#         if temp_path and os.path.exists(temp_path):
#             try:
#                 os.remove(temp_path)
#             except Exception:
#                 pass
#
#     return None
#
#
# def verify_1_to_1(uploaded_image, reference_encoding, threshold=0.30):
#     """
#     Cosine distance verification for mobile punch (1:1).
#     Distance <= 0.30 represents a strong Facenet512 match.
#     """
#     live_encoding = extract_face_encoding(uploaded_image)
#     if not live_encoding:
#         return False, "No face detected in live photo. Please face the camera directly."
#
#     known_arr = np.array(reference_encoding, dtype=np.float64)
#     live_arr = np.array(live_encoding, dtype=np.float64)
#
#     # Cosine distance
#     cosine_dist = 1.0 - (np.dot(known_arr, live_arr) / (np.linalg.norm(known_arr) * np.linalg.norm(live_arr)))
#     is_match = bool(cosine_dist <= threshold)
#     return is_match, f"Distance: {cosine_dist:.2f}"
#
#
# def match_1_to_n(uploaded_image, all_biometrics, threshold=0.30):
#     """
#     Cosine distance search for kiosk mode (1:N).
#     """
#     live_encoding = extract_face_encoding(uploaded_image)
#     if not live_encoding:
#         return None, "No face detected."
#
#     live_arr = np.array(live_encoding, dtype=np.float64)
#     norm_live = np.linalg.norm(live_arr)
#
#     best_match = None
#     min_dist = 1.0
#
#     for biometric in all_biometrics:
#         known_arr = np.array(biometric.face_encoding, dtype=np.float64)
#         norm_known = np.linalg.norm(known_arr)
#
#         cosine_dist = 1.0 - (np.dot(known_arr, live_arr) / (norm_known * norm_live))
#
#         if cosine_dist <= threshold and cosine_dist < min_dist:
#             min_dist = cosine_dist
#             best_match = biometric.employee
#
#     if best_match:
#         return best_match, f"Matched with distance {min_dist:.2f}"
#
#     return None, "Face not recognized in employee records."
#
#
# def _preprocess_fast(image_file):
#     try:
#         image_file.seek(0)
#     except Exception:
#         pass
#     pil_img = Image.open(io.BytesIO(image_file.read()))
#     try:
#         pil_img = ImageOps.exif_transpose(pil_img)
#     except Exception:
#         pass
#     pil_img = pil_img.convert("RGB")
#     # Resize to 400px max dimension for near-instant CPU inference
#     pil_img.thumbnail((400, 400), Image.Resampling.BILINEAR)
#     return np.array(pil_img)


import numpy as np
from PIL import Image, ImageOps
import io
import tempfile
import os
from deepface import DeepFace

# Fast OpenCV detector on resized frames (~80ms on CPU)
DETECTOR_BACKEND = "opencv"
MODEL_NAME = "Facenet512"


def _save_file_to_temp_jpeg(image_file):
    try:
        image_file.seek(0)
    except Exception:
        pass

    pil_img = Image.open(io.BytesIO(image_file.read()))

    try:
        pil_img = ImageOps.exif_transpose(pil_img)
    except Exception:
        pass

    rgb_img = pil_img.convert("RGB")

    # Downscale to 400px so CPU vector extraction takes under 0.2 seconds
    max_dim = 400
    if max(rgb_img.size) > max_dim:
        rgb_img.thumbnail((max_dim, max_dim), Image.Resampling.BILINEAR)

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    rgb_img.save(temp_file.name, format="JPEG", quality=90)
    temp_file.close()
    return temp_file.name


def extract_face_encoding(image_file):
    """Fast extraction using OpenCV detector with fallback."""
    temp_path = None
    try:
        temp_path = _save_file_to_temp_jpeg(image_file)

        # Primary Fast Pass
        try:
            embeddings = DeepFace.represent(
                img_path=temp_path,
                model_name=MODEL_NAME,
                detector_backend=DETECTOR_BACKEND,
                enforce_detection=True,
                align=False
            )
            if embeddings and len(embeddings) > 0:
                return embeddings[0]["embedding"]
        except Exception:
            pass

        # Fallback Pass without strict enforcement
        try:
            embeddings = DeepFace.represent(
                img_path=temp_path,
                model_name=MODEL_NAME,
                detector_backend="opencv",
                enforce_detection=False,
                align=False
            )
            if embeddings and len(embeddings) > 0:
                return embeddings[0]["embedding"]
        except Exception:
            pass

    except Exception as e:
        print(f"[Biometric Error]: {e}")
        return None
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    return None


def verify_1_to_1(uploaded_image, reference_encoding, threshold=0.45):
    """Cosine distance verification for mobile punch (1:1)."""
    live_encoding = extract_face_encoding(uploaded_image)
    if not live_encoding:
        return False, "No face detected in photo. Please look straight into the camera."

    known_arr = np.array(reference_encoding, dtype=np.float64)
    live_arr = np.array(live_encoding, dtype=np.float64)

    cosine_dist = 1.0 - (np.dot(known_arr, live_arr) / (np.linalg.norm(known_arr) * np.linalg.norm(live_arr)))
    is_match = bool(cosine_dist <= threshold)
    return is_match, f"Distance: {cosine_dist:.2f}"


def match_1_to_n(uploaded_image, all_biometrics, threshold=0.45):
    """Optimized 1:N matching across all enrolled staff."""
    live_encoding = extract_face_encoding(uploaded_image)
    if not live_encoding:
        return None, "No face detected."

    live_arr = np.array(live_encoding, dtype=np.float64)
    norm_live = np.linalg.norm(live_arr)
    if norm_live == 0:
        return None, "Invalid facial vector."

    best_match = None
    min_dist = 1.0

    for biometric in all_biometrics:
        known_arr = np.array(biometric.face_encoding, dtype=np.float64)
        norm_known = np.linalg.norm(known_arr)
        if norm_known == 0:
            continue

        cosine_dist = 1.0 - (np.dot(known_arr, live_arr) / (norm_known * norm_live))

        if cosine_dist <= threshold and cosine_dist < min_dist:
            min_dist = cosine_dist
            best_match = biometric.employee

    if best_match:
        return best_match, f"Matched with distance {min_dist:.2f}"

    return None, "Face not recognized in employee records."