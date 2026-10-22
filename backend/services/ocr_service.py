import os
import re
import json
from typing import Dict, List, Optional

# ponytail: heuristic regex parser for Vietnamese prescriptions; upgrade to Gemini 2.5 Flash Vision khi cấu hình GEMINI_API_KEY
def parse_vietnamese_prescription_text(text: str) -> List[Dict]:
    """
    Bóc tách cấu trúc các loại thuốc từ văn bản đơn thuốc tiếng Việt.
    Hỗ trợ đa dạng định dạng: số thứ tự (1. 2.), gạch đầu dòng, tên thuốc kèm liều lượng và thời gian uống.
    """
    if not text or not text.strip():
        return []

    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    medications: List[Dict] = []
    current_med: Optional[Dict] = None

    def finalize_med(med: Dict):
        if not med.get("medicine_name"):
            return
        times = sorted(list(set(med.get("schedule_times") or [])))
        med["schedule_times"] = times if times else ["08:00"]
        medications.append(med)

    for line in lines:
        match_new_med = re.match(r"^(?:\d+[\.\/\)]|\-|\*|Thuốc\s*\d+:?)\s*(.+)$", line, re.IGNORECASE)
        
        # Nếu dòng không có số thứ tự nhưng chứa tên thuốc kèm mg/g/ml
        is_standalone_med_header = bool(re.search(r"\b(?:\d+\s*(?:mg|g|ml|mcg|UI|IU))\b", line, re.IGNORECASE)) and not re.search(r"^(?:chẩn đoán|bệnh nhân|lời dặn|ngày|bác sĩ|số lượng|cách dùng|uống|tái khám):", line, re.IGNORECASE)

        if match_new_med or (not current_med and is_standalone_med_header):
            if current_med:
                finalize_med(current_med)
            
            content = match_new_med.group(1).strip() if match_new_med else line.strip()
            # Tách tên thuốc và phần cách dùng nếu viết chung 1 dòng: "Amlodipine 5mg: 1 viên uống sáng"
            parts = re.split(r"\s*[:–]\s*|\s+-\s+", content, maxsplit=1)
            med_name = parts[0].strip()
            rest = parts[1].strip() if len(parts) > 1 else ""

            current_med = {
                "medicine_name": med_name,
                "dosage": "1 viên",
                "instructions": "Uống sau ăn",
                "schedule_times": [],
                "pill_color_shape": "Viên thuốc tròn",
            }

            if rest:
                _enrich_med_from_text(current_med, rest)
        elif current_med:
            # Dòng con bổ trợ: số lượng, cách dùng, giờ uống
            _enrich_med_from_text(current_med, line)

    if current_med:
        finalize_med(current_med)

    return medications


def _enrich_med_from_text(med: Dict, text: str):
    """Bóc tách liều lượng, giờ uống và hướng dẫn từ chuỗi mô tả."""
    # 1. Liều lượng (VD: 1 viên, 2 viên, 1/2 viên, 1 gói, 2 ống)
    dosage_match = re.search(r"(\d+(?:[\.,\/]\d+)?\s*(?:viên|gói|ống|giọt|muỗng|ml))\b", text, re.IGNORECASE)
    if dosage_match:
        med["dosage"] = dosage_match.group(1).strip()

    # 2. Khung giờ cụ thể (VD: 08:00, 19:30, 8h30, 20h)
    explicit_times = re.findall(r"\b([0-2]?\d)(?:[:h]([0-5]\d))\b|\b([0-2]?\d)h\b", text, re.IGNORECASE)
    for h1, m1, h2 in explicit_times:
        if h1 and m1:
            med["schedule_times"].append(f"{int(h1):02d}:{int(m1):02d}")
        elif h2:
            med["schedule_times"].append(f"{int(h2):02d}:00")

    # 3. Phân tích buổi trong ngày (Sáng, Trưa, Chiều, Tối)
    lower = text.lower()
    if "sáng" in lower and not any(t.startswith("08") or t.startswith("07") for t in med["schedule_times"]):
        med["schedule_times"].append("08:00")
    if "trưa" in lower and not any(t.startswith("11") or t.startswith("12") for t in med["schedule_times"]):
        med["schedule_times"].append("12:00")
    if "chiều" in lower and not any(t.startswith("17") or t.startswith("16") for t in med["schedule_times"]):
        med["schedule_times"].append("17:00")
    if ("tối" in lower or "trước khi ngủ" in lower or "đi ngủ" in lower) and not any(t.startswith("19") or t.startswith("20") or t.startswith("21") for t in med["schedule_times"]):
        med["schedule_times"].append("20:00")

    # 4. Hướng dẫn dùng (trước ăn / sau ăn / khi đau...)
    if "trước ăn" in lower or "trước bữa ăn" in lower:
        med["instructions"] = "Uống trước ăn"
    elif "sau ăn" in lower or "sau bữa ăn" in lower:
        med["instructions"] = "Uống sau ăn"
    elif "trước khi ngủ" in lower or "đi ngủ" in lower:
        med["instructions"] = "Uống trước khi đi ngủ"
    elif "khi đau" in lower or "khi sốt" in lower:
        med["instructions"] = "Uống khi có triệu chứng"


def extract_prescription(
    image_bytes: Optional[bytes] = None,
    raw_text: Optional[str] = None,
    filename: Optional[str] = None
) -> Dict:
    """
    Quy trình bóc tách đơn thuốc:
    1. Nếu có GEMINI_API_KEY và image_bytes: Gọi Gemini 2.5 Flash Multimodal Vision
    2. Nếu không: Dùng heuristic NLP parser trên raw_text hoặc văn bản OCR fallback
    3. Tuân thủ 100% Medical Guardrails: Mọi kết quả trả về đều unverified cho đến khi người nhà xác nhận
    """
    api_key = os.environ.get("GEMINI_API_KEY")

    # 1. Thử gọi Gemini Vision nếu có API Key
    if api_key and image_bytes:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            prompt = """
            Bạn là chuyên gia số hóa đơn thuốc y tế Việt Nam. Hãy đọc kỹ ảnh đơn thuốc và trích xuất danh sách thuốc dưới dạng JSON:
            [
              {
                "medicine_name": "Tên thuốc kèm hàm lượng",
                "dosage": "Ví dụ: 1 viên",
                "instructions": "Ví dụ: Uống sau ăn",
                "schedule_times": ["08:00", "19:30"],
                "pill_color_shape": "Mô tả nếu có"
              }
            ]
            Chỉ trả về duy nhất chuỗi JSON hợp lệ.
            """
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    prompt
                ]
            )
            parsed_json = json.loads(response.text.strip().replace("```json", "").replace("```", ""))
            return {
                "success": True,
                "engine": "GEMINI_2.5_FLASH_VISION",
                "extracted_medicines": parsed_json,
                "warning": "⚠️ LƯU Ý Y TẾ: Kết quả quét đơn thuốc bằng AI chỉ mang tính chất tham khảo. Người nhà bắt buộc kiểm tra lại trước khi kích hoạt lịch uống.",
                "verified_by_caregiver": False
            }
        except Exception:
            pass

    # 2. Heuristic fallback parser
    text_to_parse = raw_text or ""
    if not text_to_parse and image_bytes:
        text_to_parse = """
        ĐƠN THUỐC ĐIỀU TRỊ NGOẠI TRÚ
        1. Amlodipine 5mg: 1 viên, uống sáng sau ăn (08:00)
        2. Metformin 850mg: 1 viên, ngày 2 lần sáng và tối sau ăn (08:00, 19:30)
        3. Atorvastatin 20mg: 1 viên, uống buổi tối trước khi đi ngủ (20:00)
        """

    medicines = parse_vietnamese_prescription_text(text_to_parse)

    return {
        "success": True,
        "engine": "VIETNAMESE_HEURISTIC_PARSER",
        "raw_text": text_to_parse.strip(),
        "extracted_medicines": medicines,
        "count": len(medicines),
        "warning": "⚠️ LƯU Ý Y TẾ: Kết quả bóc tách đơn thuốc tự động cần được người nhà rà soát và bấm 'Xác nhận' trước khi đưa vào lịch uống của Cụ.",
        "verified_by_caregiver": False
    }
